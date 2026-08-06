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
was verified during Task 17 (see report). To see real suggestion cards, set:

```
EPICVIBE_INFERENCE_PROVIDER=anthropic
EPICVIBE_ANTHROPIC_API_KEY=<your key>
```

in `.env` and restart the server before repeating the POSTs above.

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
shortlist will legitimately come back empty (expected miss, not a bug) — the fixtures in
`fixtures/hooks/` are the reliable path for exercising the "hit" case locally.

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

This is the integration milestone from spec §11 stage 2. Checklist:

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
