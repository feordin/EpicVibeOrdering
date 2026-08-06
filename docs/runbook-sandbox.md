# Sandbox Runbook

Practical steps to run EpicVibe end-to-end at each stage of the rollout path (see design
spec §11): local smoke test, the public CDS Hooks Sandbox, an Epic FHIR sandbox for
payload-shape validation, and the customer's non-production Epic environment.

## 1. Local smoke

Start the server (defaults: `EPICVIBE_INFERENCE_PROVIDER=fake`, `EPICVIBE_VERIFY_JWT=false`,
sample catalog):

```bash
uvicorn epicvibe.cds.app:create_app --factory --port 8000
```

Confirm discovery lists the three services:

```bash
curl -s http://localhost:8000/cds-services
```

Expected: a `services` array with `epicvibe-patient-view`, `epicvibe-order-select`, and
`epicvibe-order-sign`, each carrying a `prefetch` block for `conditions` and
`medications`.

POST the `patient-view` fixture to warm the cache (this always returns immediately —
`patient-view` is the warm-up trigger per spec §6, it never renders suggestion cards
itself):

```bash
curl -s -X POST http://localhost:8000/cds-services/epicvibe-patient-view \
  -H "Content-Type: application/json" \
  -d @fixtures/hooks/patient_view.json
```

Expected: `{"cards": []}` immediately (a background inference job is enqueued and its
result is cached under the encounter key).

POST the `order-select` fixture (same `patientId`/`encounterId` as the fixture above, so
it hits the same cache key):

```bash
curl -s -X POST http://localhost:8000/cds-services/epicvibe-order-select \
  -H "Content-Type: application/json" \
  -d @fixtures/hooks/order_select.json
```

- If the background job from the `patient-view` POST hasn't finished yet, this also
  returns `{"cards": []}` and enqueues its own job.
- Once the job completes, subsequent POSTs render suggestion cards for whatever the
  proposal engine grounded against the catalog.

**With the default `fake` inference provider, the cached proposal is empty**, so
`order-select` keeps returning `{"cards": []}` even after warm-up — this is expected and
was verified during Task 17 (see report). Two ways to see real suggestion cards:

```
EPICVIBE_INFERENCE_PROVIDER=demo        # deterministic DM2 proposal, no API key needed
# or
EPICVIBE_INFERENCE_PROVIDER=anthropic
EPICVIBE_ANTHROPIC_API_KEY=<your key>
```

in `.env` and restart the server before repeating the POSTs above. The `demo` provider
returns a fixed, catalog-grounded diabetes proposal for any patient — ideal for demos
and the accept-flow walkthrough below.

### Epic-dialect contract check (Layer 1)

With the server running (use `demo` provider), replay Epic-shaped requests — including
Epic's `com.epic.cdshooks.request.*` extensions, PractitionerRole user, and
contained-Medication draft orders — and validate every response against Epic's
documented suggestion-card rules (one action per suggestion, `selectionBehavior "any"`,
honored code systems, etc.):

```bash
python -m epicvibe.replay --url http://localhost:8000
```

Expected: `OK` lines for all three hooks, exit code 0. Any violation names the exact
Epic rule breached. Run this after ANY change to the card emitters or hook handlers.

For an interactive demo instead of raw curl, use the CDS Hooks Sandbox flow (§2 below).

## 2. CDS Hooks Sandbox

The [CDS Hooks Sandbox](https://sandbox.cds-hooks.org) is the interactive demo path — it
drives `patient-view`/`order-select` from its own UI and renders cards visually.

1. Expose the local server publicly with a tunnel:

   ```bash
   cloudflared tunnel --url http://localhost:8000
   # or: ngrok http 8000
   ```

2. Open https://sandbox.cds-hooks.org and add the tunnel's HTTPS URL as a CDS service
   endpoint (the sandbox will call `GET <url>/cds-services` to discover services).
3. Select a patient and step through the sandbox's patient-view / order-select flow.
   Verify cards render for both hooks.
4. Keep `EPICVIBE_VERIFY_JWT=false` for this stage — the sandbox does not present a
   verifiable Epic-issued JWT.

Note: the sandbox sends its own synthetic FHIR test data via prefetch. Unless the
selected patient happens to have an active diabetes `Condition`, the proposal engine's
shortlist will legitimately come back empty (expected miss, not a bug) — run with
`EPICVIBE_INFERENCE_PROVIDER=demo` to guarantee cards regardless of the sandbox
patient (the demo provider always returns the DM2 proposal), or use the fixtures in
`fixtures/hooks/` for the organic "hit" case locally.

### Accept-flow walkthrough (Layer 2: simulated write-back)

This exercises the full suggestion-accept loop — the closest public approximation of
Epic filing an unsigned order from our card:

1. Run the server with `EPICVIBE_INFERENCE_PROVIDER=demo` and the tunnel from step 1.
2. In the sandbox, open a patient chart — this fires `patient-view` at our service
   (warm-up; no cards expected on the first fire).
3. Switch to the **Rx View** (or order entry view) and select any medication — this
   fires `order-select`. Our suggestion card should render with the five DM2 items,
   each pre-checked (`isRecommended`).
4. Click **Accept** on a suggestion. The sandbox applies the suggestion's `create`
   action — the draft `ServiceRequest`/`MedicationRequest` from our card is written to
   the sandbox's open FHIR server. This is the same mechanical contract Epic honors
   (Epic additionally resolves our code against its own orderables and composes
   details from its build — that part is only observable in a real Epic, §4).
5. Verify the write: query the sandbox FHIR server for the created resource (the
   sandbox UI shows the request; or GET the resource type filtered by patient).
6. Watch our server logs / audit store for the `feedback` POST if the sandbox sends
   one (Epic does in production; the public sandbox may not — absence here is not a
   failure).

What this proves: our cards are spec-valid, render correctly, and their actions apply
cleanly. What it cannot prove: Epic's orderable resolution, Epic-side default
composition, and OPA presentation — those are §4 items.

## 3a. Meld sandbox (Layer 3: EHR-style demo)

[Meld](https://meld.interop.community) (successor to the retired Logica sandbox) offers
an EHR-like chart UI with CDS Hooks support and a persistent FHIR server — better
stakeholder demos than the CDS Hooks Sandbox's developer UI.

Setup (requires a free Meld account — **user action, one-time**):

1. Create a Meld account and a sandbox (R4).
2. In the sandbox's **CDS Hooks** settings, register our tunnel URL as a CDS service
   (Meld reads the discovery endpoint like Epic would).
3. Load or pick a patient with a diabetes condition (Meld supports importing synthetic
   patients — Synthea bundles work), or run the `demo` provider to force cards.
4. Open the patient chart to fire `patient-view`; use Meld's medication/order UI to
   fire `order-select`; accept a suggestion and verify the created draft resource in
   Meld's FHIR server (Data Manager view).
5. Same JWT note as §2: keep `EPICVIBE_VERIFY_JWT=false`.

## 3. Epic FHIR sandbox (payload fidelity)

Use Epic's free developer sandbox to validate that our fixtures match real Epic R4 shapes,
not to fire CDS Hooks (Epic's public sandbox cannot do that — see §5).

1. Register a free app at https://fhir.epic.com.
2. Use the sandbox's test patients (e.g., Camila Lopez) to pull real
   `Condition` and `MedicationRequest` resources via the FHIR R4 API.
3. Diff the real payload shapes (coding systems, status vocab, nesting) against
   `fixtures/hooks/*.json` and `fixtures/catalog/sample_catalog.json`. Adjust fixtures
   wherever Epic's actual shapes differ from our assumptions — this is the main value of
   this stage, since it de-risks the customer non-prod integration in §4.

## 4. Customer non-prod (the real milestone)

This is the integration milestone from spec §11 stage 2.

**Note:** the in-memory proposal cache and the job runner are per-process state — run a
single uvicorn worker for Phase 1. Multi-worker (or multi-instance) deployment would
split this state across processes and break the warm-up cache/job flow; it requires a
shared cache (e.g. Redis) and is a Phase 2+ concern.

Checklist:

- [ ] Client ID registered with the customer's Epic instance (free registration via
      fhir.epic.com, scoped to their org).
- [ ] CDS service endpoint URIs registered **exactly** as our discovery response returns
      them (`/cds-services/epicvibe-patient-view`, `/cds-services/epicvibe-order-select`,
      `/cds-services/epicvibe-order-sign`) — Epic does not tolerate trailing-slash or
      casing mismatches.
- [ ] Customer analysts have built the OPA (OurPractice Advisory) record for all three
      hooks, using the discovery `prefetch` block verbatim:

  ```json
  {
    "conditions": "Condition?patient={{context.patientId}}&clinical-status=active",
    "medications": "MedicationRequest?patient={{context.patientId}}&status=active"
  }
  ```

- [ ] `EPICVIBE_VERIFY_JWT=true` set, with `EPICVIBE_JWKS_URL` pointed at the customer's
      Epic JWKS endpoint and `EPICVIBE_JWT_AUDIENCE` set to our registered client ID.
- [ ] Confirm with the customer's analysts that `patient-view` is configured to fire at
      chart open — this is the warm-up trigger (spec §6); if it doesn't fire, the
      background inference job never starts and `order-select`/`order-sign` will never
      have a cached proposal to render from. Epic advises sites against over-firing
      `patient-view`, so this needs explicit site alignment, not just a default OPA
      setting.
- [ ] Measure handler latency end-to-end (`patient-view` response time and time-to-cache
      for the background job) against real customer data volumes, not just the sample
      catalog.

## 5. Known limitation

**Epic's public sandbox (fhir.epic.com / the "sandbox" app registration) cannot fire CDS
Hooks.** It only supports direct FHIR API calls for payload-shape validation (§3 above).
Do not attempt to test `patient-view`/`order-select`/`order-sign` firing against it — use
the CDS Hooks Sandbox (§2) for interactive hook testing, and the customer's non-prod Epic
(§4) for the real integration.
