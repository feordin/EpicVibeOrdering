# Runbook: containerised reference-standards demo

A **second** demo of the EpicVibe CDS service that replaces our in-repo mock EHR with
third-party reference implementations of the standards. The in-repo mock EHR
(`python -m epicvibe.mockehr`, `docs/runbook-demo.md`) stays the primary demo — it is the
only one that models Epic-specific behaviour. This one exists to prove the service is
spec-conformant against implementations nobody on this project wrote.

Everything lives in `demo/reference-sandbox/`.

| Component | Image / source | Host port | URL |
|---|---|---|---|
| HAPI FHIR R4 | `hapiproject/hapi:v8.0.0` | 8080 | <http://localhost:8080/fhir> |
| `fhir-root` shim | `nginx:1.27-alpine` | – | internal only (see below) |
| SMART App Launcher v2 | `smartonfhir/smart-launcher-2:latest` | 8090 | <http://localhost:8090> |
| CDS Hooks Sandbox | built here from `cds-hooks/sandbox` @ `b52fc1b` | 8095 | <http://localhost:8095> |
| seed (one-shot) | `python:3.12-alpine` + `seed/seed.py` | – | – |
| EpicVibe CDS service | **host process** (or `--profile cds`) | 8000 | <http://localhost:8000/cds-services> |

The FHIR base the browser app uses is the launcher's R4 proxy —
`http://localhost:8090/v/r4/fhir` — not HAPI directly, so a SMART launch of our app
goes through real OAuth2.

Hostnames: the **browser** always uses `localhost`. **Containers** reach each other by
service name (`http://hapi:8080/fhir`, `http://fhir-root`) and would reach the host via
`host.docker.internal` (only relevant if you run the CDS service in compose).

---

## 1. Bring the stack up

```powershell
cd demo/reference-sandbox
docker compose up -d --build          # first build of cds-sandbox takes ~5-10 min
docker compose run --rm seed          # loads the three patients + practitioner
```

If the `cds-sandbox` build fails inside `npm ci` with
`npm error Exit handler never called!`, your network cannot reach
`registry.npmjs.org` from inside Docker. Copy `.env.example` to `.env` and set
`NPM_REGISTRY` to whatever `npm config get registry` reports on the host; the build
arg is threaded through `docker-compose.yml`. (That is the case on this machine — the
committed `.env.example` shows the shape, and `.env` itself is gitignored.)

`seed` waits for HAPI itself (polls `/fhir/metadata` for up to `WAIT_SECONDS`, default
300s), so it is safe to run immediately. It prints a per-file resource count and then
verifies `Patient/pat-okafor`, `Patient/pat-santos`, `Patient/pat-brooks`,
`Practitioner/pr-1`. Expected output:

```
[seed] HAPI ready at http://hapi:8080/fhir (after 9 attempts)
[seed] practitioners.json: 2 resources upserted
[seed] santos.json: 15 resources upserted
[seed] okafor.json: 17 resources upserted
[seed] brooks.json: 18 resources upserted
[seed] verify Patient/pat-okafor: OK  (HTTP 200)
...
[seed] done
```

### How seeding works

`fixtures/mockehr/patients/*.json` are FHIR **collection** Bundles, which a server will
not accept. `seed/seed.py` rewrites each one into a **transaction** Bundle whose entries
are `PUT <ResourceType>/<id>` upserts, which preserves the literal ids the whole demo
depends on (`pat-okafor`, `pr-1`, …). HAPI only honours non-numeric client-supplied ids
with `hapi.fhir.client_id_strategy=ANY`, which `docker-compose.yml` sets.
Practitioners/Organizations are loaded first because patient resources reference them.
Re-running `seed` is idempotent (PUT upserts).

## 2. Start the EpicVibe CDS service on the host

Kept out of compose so the dev loop stays fast.

```powershell
$env:EPICVIBE_INFERENCE_PROVIDER = "demo"
$env:EPICVIBE_SMART_ALLOWED_ISSUERS = '["http://localhost:8090/v/r4/fhir","http://127.0.0.1:8090/v/r4/fhir","http://localhost:8100/fhir","http://127.0.0.1:8100/fhir"]'
.venv\Scripts\python -m uvicorn epicvibe.cds.app:create_app --factory --host 0.0.0.0 --port 8000
```

Optionally in compose instead: `docker compose --profile cds up -d --build cds`
(uses the repo-root `Dockerfile`).

### CORS

The sandbox is a browser SPA on `http://localhost:8095` calling our service on
`http://localhost:8000` with an `Authorization: Bearer <JWT>` header, so every call is a
preflighted CORS request. `Settings.cors_allow_origins` (new;
`EPICVIBE_CORS_ALLOW_ORIGINS`) defaults to
`["http://localhost:8095","http://localhost:8090","http://localhost:8100"]` and
`src/epicvibe/cds/app.py` installs `CORSMiddleware` for it. Setting it to `[]` disables
CORS entirely. Covered by `tests/test_cors.py`.

Verify by hand:

```powershell
curl -i -X OPTIONS -H "Origin: http://localhost:8095" `
  -H "Access-Control-Request-Method: POST" `
  -H "Access-Control-Request-Headers: authorization,content-type" `
  http://localhost:8000/cds-services/epicvibe-patient-view
# -> 200, access-control-allow-origin: http://localhost:8095
```

## 3. Click-through

Open the sandbox pre-configured via query parameters (all URL-encoded):

```
http://localhost:8095/?fhirServiceUrl=http%3A%2F%2Flocalhost%3A8090%2Fv%2Fr4%2Ffhir&serviceDiscoveryURL=http%3A%2F%2Flocalhost%3A8000%2Fcds-services&patientId=pat-okafor
```

Equivalent by hand: gear menu -> **Change FHIR Server** -> `http://localhost:8090/v/r4/fhir`;
gear menu -> **Add CDS Services** -> `http://localhost:8000/cds-services`; gear menu ->
**Change Patient** -> `pat-okafor`. Both paths persist to `localStorage`
(`PERSISTED_fhirServer`, `PERSISTED_cdsServices`, `PERSISTED_patientId`), so a later plain
`http://localhost:8095/` reuses them; gear -> **Reset Configuration** clears them.

Then:

1. **Patient View** tab — the `patient-view` hook fires. The *first* call returns
   `{"cards":[]}` (our service enqueues generation and returns immediately), so the card
   appears on a later invocation. The sandbox only re-invokes CDS when the hook context
   changes, so bounce **Rx View -> Patient View** to re-fire.
2. **Rx View** tab — `order-select`. It does **not** fire until a medication is chosen
   from the type-ahead (the draft `MedicationRequest` is only built once
   `medListPhase === 'done'`). Type e.g. `amoxicillin` and pick an entry.
3. Suggestion buttons render as MUI buttons on the card; **Rx Sign** exercises
   `order-sign`.

A scripted version of exactly this is `demo/reference-sandbox/drive_sandbox.py`
(Playwright, chromium from `.venv`), which also writes
`demo/reference-sandbox/screenshots/*.png`.

---

## 4. What the CDS Hooks Sandbox actually supports

Read from the pinned source (`b52fc1b`), not assumed:

| Behaviour | Reality |
|---|---|
| Default FHIR server | `https://launch.smarthealthit.org/v/r2/fhir` (**DSTU2**). It reads `fhirVersion` from `/metadata` and adapts; against our R4 server it correctly emits `MedicationRequest`/`subject`/`authoredOn` (`createFhirResource`, `compareVersions(fhirVersion,'3.0.1') >= 0`). |
| Adding a CDS service | Both UI (gear -> *Add CDS Services*) and `?serviceDiscoveryURL=` (comma-separated, URL-encoded). Expects the full discovery URL, i.e. `.../cds-services`. |
| Auth to our service | Sends `Authorization: Bearer <self-signed RS256 JWT>` on discovery, hook and feedback calls (`jwt-generator.js`, `iss: https://sandbox.cds-hooks.org`). Our service accepts it because `verify_jwt` is off by default. |
| Prefetch | Implements the spec's prefetch templates (`{{context.*}}`, `{{userPractitionerId}}`, `{{today()}}`) and tries `POST <Type>/_search` first, falling back to `GET`. Tokens it cannot resolve cause the prefetch key to be **skipped with a console warning** — so our `encounter: Encounter/{{context.encounterId}}` is always dropped here (the sandbox has no encounter concept). |
| `order-select` with `draftOrders` | **Yes.** `rx-view/order-select` sends `selections: ["MedicationRequest/request-123"]` and `draftOrders` as a Bundle with the single draft resource. `pama` screen does the same for `ServiceRequest`. |
| `order-sign` | Yes, from the **Rx Sign** screen, with the same single-draft Bundle. |
| Accept applying `create` actions to the FHIR server | **No.** `takeSuggestion` never writes to FHIR. On `rx-view` it only re-points the in-UI prescribed medication *if* the suggestion action carries `resource.medicationCodeableConcept` with `text` + `coding[0].code`; anything else logs a console warning and is dropped. On the **patient-view** screen `takeSuggestion` is literally `() => {}` — Accept is a no-op. `ServiceRequest` creates are ignored entirely. |
| Feedback | **Yes** — Accept POSTs `{feedback:[{card, outcome:"accepted", acceptedSuggestions:[{id}]}]}` to `<serviceUrl>/feedback`, and Dismiss posts `outcome:"overridden"` (with `overrideReason` when the card supplies `overrideReasons`). Our service records these in the audit store. |
| `smart` links | Attempts them, but requires the FHIR server to implement the **HSPC-era** `POST <fhirBase>/_services/smart/Launch` endpoint to mint a `launch_id`. Neither HAPI nor the SMART App Launcher v2 has that endpoint, so the button renders **disabled** with the tooltip *"Cannot launch SMART link without a SMART-enabled FHIR server"*. |
| `absolute` / `external` links | Plain links work normally. |
| `systemActions` | Handled via `extension.systemActions` per trigger point (our service does not emit any). |
| Long-running services | Honours a `potentiallyLongRunning` discovery indicator by showing a spinner. Our discovery does not set it — which is why the first `patient-view` call looks empty rather than pending. |

## 5. Verified behaviours (this run)

- `docker compose up -d --build` brings up HAPI, the launcher and the sandbox.
- Seeding: 52 resources across 4 fixture files, all four verification reads 200.
- The launcher's R4 proxy serves our seeded data **unauthenticated**:
  `GET http://localhost:8090/v/r4/fhir/Patient/pat-okafor` -> 200, and both
  `GET /Condition?patient=pat-okafor` and `POST /Condition/_search` -> `searchset` with 3
  conditions. (The sandbox is an "open launch" client and refuses secured endpoints.)
- CORS preflight from `http://localhost:8095` -> 200 with
  `access-control-allow-origin: http://localhost:8095` and `authorization` allowed.
- `patient-view` end-to-end against the launcher FHIR base: first POST -> `{"cards":[]}`,
  second POST (~8s later) -> one card,
  `"AI order review ready: 9 suggested orders"`, matched order set *Community-Acquired
  Pneumonia - Admission (ED/Inpatient)* — i.e. the background FHIR pull against the
  launcher proxy worked with no prefetch supplied at all.

### Click-through, driven with Playwright (`drive_sandbox.py`)

Screenshots in `demo/reference-sandbox/screenshots/`:
`01-patient-view-card.png`, `02-order-select-suggestions.png`, `03-after-accept.png`.

- Query-parameter bootstrap worked: patient banner showed **Okafor**, one discovery
  `GET http://localhost:8000/cds-services`.
- **patient-view card rendered** after one Rx View -> Patient View bounce (poll #1).
- **order-select card rendered** after typing `amoxicillin` (30 matches offered) and
  picking the first. The card carried **9 suggestion buttons** — *CBC with Differential,
  Basic Metabolic Panel, Blood Culture x2 sets, Procalcitonin, Chest X-Ray 2 views,
  cefTRIAXone 1 g IV q24h, azithromycin 500 mg IV daily, Continuous SpO2 monitoring,
  Droplet isolation precautions* — plus the `Open EpicVibe Order Assistant` SMART link.
- **Accept**: clicked a `ServiceRequest` suggestion (*CBC with Differential*) and a
  `MedicationRequest` suggestion (*cefTRIAXone 1 g IV q24h*). Both POSTed
  `outcome:"accepted"` feedback to `/cds-services/epicvibe-order-select/feedback`
  (200, 2 calls). **Neither changed the UI** and neither re-fired `order-select`:
  - the ServiceRequest one logs `Suggested resource does not have a
    medicationCodeableConcept`; `ServiceRequest` suggestions are still ignored by this
    client regardless of shape;
  - the MedicationRequest one now carries `medicationCodeableConcept.text` alongside
    `coding[0].code`, so medications apply in the Rx view on Accept.
- **SMART link**: the `Open EpicVibe Order Assistant` button is rendered **disabled**
  with *"Cannot launch SMART link without a SMART-enabled FHIR server"*; the console
  shows `POST <fhirBase>/_services/smart/Launch` -> 400.
- Console also confirms `Skipping prefetch "encounter" due to unresolved tokens:
  Encounter/{{context.encounterId}}` on every exchange, and that the sandbox
  additionally loads its own default discovery endpoint
  `https://sandbox-services.cds-hooks.org/cds-services` (its hosted `pama-imaging`
  service then 500s — harmless noise, unrelated to our service).

### SMART on FHIR launch through the launcher (real OAuth) — works

Not via a card link (see above) but via the launcher's provider-EHR launch:

```
http://localhost:8090/launcher?launch_uri=http%3A%2F%2Flocalhost%3A8000%2Fsmart%2Flaunch&fhir_ver=4&patient=pat-okafor&provider=pr-1
```

Following the redirect chain ends at `http://localhost:8000/smart/app` (HTTP 200,
the EpicVibe Order Assistant page) having gone through
`/smart/launch` -> `<launcher>/v/r4/auth/authorize` (PKCE S256, `aud=http://localhost:8090/v/r4/fhir`)
-> `/smart/callback`. This requires
`EPICVIBE_SMART_ALLOWED_ISSUERS` to contain `http://localhost:8090/v/r4/fhir`.

#### Why there is a `fhir-root` nginx shim

The launcher's provider-EHR launch auto-selects an encounter with
`new URL("/Encounter/?_count=1&_sort:desc=date", FHIR_SERVER_R4)`
(`backend/routes/auth/authorize.ts::getFirstEncounterId`). The leading slash makes
`URL()` discard any **path** on `FHIR_SERVER_R4`, so `http://hapi:8080/fhir` becomes a
request to `http://hapi:8080/Encounter` -> 404 -> HTTP 400
`"Failed to auto-select the first encounter for patient with id of 'pat-okafor'"`.
`fhir-root.conf` is a one-location nginx reverse proxy that re-publishes HAPI's `/fhir`
base at the root of its own origin, so `FHIR_SERVER_R4=http://fhir-root` is path-less and
the launcher's assumption holds. (The launcher's own FHIR proxy handles a path fine — it
is only this encounter lookup that breaks.)


## 6. Known limitations

- **Accept does not file orders.** See the table above; the sandbox is a card renderer,
  not an EHR. The in-repo mock EHR remains the only place the `create`-suggestion write
  path is exercised end-to-end.
- **SMART links on cards are dead here.** The sandbox's launch-context flow predates the
  SMART App Launcher and depends on `_services/smart/Launch`. Launch our SMART app from
  the launcher instead — the `/launcher?launch_uri=...` URL in section 5 works
  end-to-end — or from the launcher UI at <http://localhost:8090>.
- **No encounter context.** The sandbox never sends `encounterId`, so our cache key falls
  back to `pat:<patientId>` and the `encounter` prefetch is skipped.
- **First hook call is always empty.** Our service generates asynchronously and the
  sandbox does not poll — you must re-trigger (switch tabs) to see the card.
- **The sandbox image build needs a reachable npm registry.** On this machine
  `registry.npmjs.org` is unreachable from inside Docker (TLS handshake failures), and
  npm reports that as `Exit handler never called!` while *exiting 0* with a half-installed
  `node_modules`. Hence: the `NPM_REGISTRY` build arg (`.env` / `.env.example`), and a
  `test -x node_modules/.bin/webpack` guard in the Dockerfile before `npm run build`.
  `node_modules` is mounted as tmpfs so the ~1.3GB tree never enters an image layer.
- **`hapiproject/hapi` is distroless** — no `/bin/sh`, no `wget`/`curl` — so a compose
  `healthcheck` can never pass (`stat /bin/sh: no such file or directory`). Readiness
  polling lives in the seed script instead.
- Only R4 is wired up; `FHIR_SERVER_R2`/`R3` are set to `""` so the launcher cannot fall
  back to the public smarthealthit.org servers.
- HAPI data is in the container's H2 database; `docker compose down` discards it, so
  re-run `seed` after every fresh `up`.

## 7. Tear down

```powershell
cd demo/reference-sandbox
docker compose down                  # stops hapi, fhir-root, smart-launcher, cds-sandbox;
                                     # keeps images; discards HAPI's H2 data
docker compose down -v --rmi local   # also drop the locally built sandbox image
```

Stop the host CDS service with Ctrl+C in its terminal.
