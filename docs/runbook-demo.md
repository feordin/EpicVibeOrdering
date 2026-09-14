# Demo Runbook

> For a ~20-minute presenter's script to run live in front of stakeholders, see
> `docs/demo-walkthrough.md`. This document is the detailed reference it points back
> to — every button, setting, and limitation.

Practical, fully-offline demo scripts for the two customer-facing scenarios EpicVibe
covers. Everything here runs with **no external API key** — the CDS service uses the
`demo` inference provider (a fixed, catalog-grounded diabetes proposal) and the downtime
subsystem uses its `fake` (`provider=fake`) keyword extractor. Optional real-LLM settings
are called out in each scenario's settings table for a live-model demo instead.

- **Scenario A** — Offline / downtime: transcript → template → HL7v2 batch.
- **Scenario B** — Online: SMART on FHIR + CDS Hooks with the mock EHR.

For a real interactive CDS Hooks client, a public CDS Hooks sandbox, Meld, an Epic FHIR
sandbox, or a customer's non-production Epic, see `docs/runbook-sandbox.md` instead —
this document only covers the local, no-external-dependency demo path.

## Run everything

Four processes, four ports. Run each in its own terminal (or background them).

**PowerShell**

```powershell
$env:EPICVIBE_INFERENCE_PROVIDER = "demo"
python -m uvicorn epicvibe.cds.app:create_app --factory --port 8000      # CDS service      :8000
python -m epicvibe.mockehr                                               # mock EHR         :8100
python -m epicvibe.downtime.mock_engine --port 2575 --inbox .downtime-inbox   # mock HL7 engine  :2575
python -m epicvibe.downtime                                              # downtime app     :8200
```

**bash**

```bash
export EPICVIBE_INFERENCE_PROVIDER=demo
python -m uvicorn epicvibe.cds.app:create_app --factory --port 8000      # CDS service      :8000
python -m epicvibe.mockehr                                               # mock EHR         :8100
python -m epicvibe.downtime.mock_engine --port 2575 --inbox .downtime-inbox   # mock HL7 engine  :2575
python -m epicvibe.downtime                                              # downtime app     :8200
```

Scenario B needs the CDS service + mock EHR (:8000, :8100). Scenario A needs the mock
HL7 engine + downtime app (:2575, :8200) — they're independent of each other and of
Scenario B.

Run the test suite at any point:

```bash
python -m pytest
```

---

## Scenario B: Online — SMART on FHIR + CDS Hooks with the mock EHR

### What's in the box

| Piece | Port | Start command |
|---|---|---|
| CDS service | 8000 | `python -m uvicorn epicvibe.cds.app:create_app --factory --port 8000` (with `EPICVIBE_INFERENCE_PROVIDER=demo`) |
| Mock EHR | 8100 | `python -m epicvibe.mockehr` |

The mock EHR (`src/epicvibe/mockehr`) is a standalone FastAPI app with its own chart UI,
FHIR store, OAuth stub, and a `HooksClient` that fires CDS Hooks at the CDS service the
same way Epic would. It is **not** Epic — see Limitations below.

Three synthetic patients ship in `fixtures/mockehr/patients/`, each driving a different
catalog order set:

| Patient | Condition | Order set driven |
|---|---|---|
| Maria Santos (`pat-santos`) | New type 2 diabetes mellitus | Diabetes Mellitus Type 2 — New Diagnosis (Ambulatory) |
| James Okafor (`pat-okafor`) | Community-acquired pneumonia | Community-Acquired Pneumonia — Admission (ED/Inpatient) |
| Evelyn Brooks (`pat-brooks`) | Heart failure exacerbation (reduced EF) | Heart Failure Exacerbation — Inpatient |

With `EPICVIBE_INFERENCE_PROVIDER=demo` the CDS service routes off the patient's problem
list (`src/epicvibe/inference/demo.py`): ICD-10 prefixes and free-text keywords pick the
DM2, CAP, heart-failure or hypertension proposal, so each patient above yields the order
set in their row. It is deterministic, not a model — the same patient always produces the
same proposal, and anything it cannot match falls back to the DM2 proposal. The
click-through below works identically for all three patients.

### Click-through script

1. Open `http://localhost:8100` — the mock EHR's patient list.
2. Pick a patient (e.g. Maria Santos).
3. **Chart open fires `patient-view`.** The mock EHR's `HooksClient` posts the
   `patient-view` hook context to the CDS service. Prefetch now includes
   `patient`/`observations`/`allergies`/`encounter` (see the discovery `prefetch` block);
   the service also pulls anything still missing directly from `fhirServer`
   (`src/epicvibe/proposal/fhir_pull.py: fetch_missing`) — fail-soft, timeout-bounded,
   never blocks the hook response.
4. A **summary card** appears after roughly 2 seconds (the background inference job
   completes and the mock EHR's dev panel/chart polls pick it up).
5. Click **New order**, add a draft order (any free-text order name).
6. Adding the draft fires `order-select` — a **suggestion card** renders with
   pre-populated `create` actions. Example of one pre-populated resource (trimmed):

   ```json
   {
     "resourceType": "MedicationRequest",
     "status": "draft", "intent": "proposal",
     "medicationCodeableConcept": {"coding": [{"code": "...", "display": "Metformin 500mg"}]},
     "dosageInstruction": [{"text": "500 mg PO BID", "timing": {"repeat": {"frequency": 2, "period": 1, "periodUnit": "d"}}}],
     "priority": "routine",
     "reasonCode": [{"text": "New type 2 diabetes mellitus"}]
   }
   ```

7. Click **Accept** on a suggestion — `POST /api/suggestions/accept` files the
   suggestion's `create` resource into the mock EHR's store as a draft order (this is
   the mechanical write-back contract, same as the sandbox accept-flow in
   `docs/runbook-sandbox.md`).
8. Click **Sign** — `POST /api/orders/sign` fires `order-sign` with the draft-order
   bundle in context; a **completeness card** renders, and the drafts flip to `active`.
9. Click **"Open EpicVibe Order Assistant"** — the suggestion/completeness cards' `smart`
   link (rendered by `epicvibe.cds.cards.smart_link`) launches the SMART app.
10. **EHR launch**: `POST /api/smart/launch` mints an OAuth launch context, then the
    browser follows through `/smart/launch` (authorize) → `/smart/callback` (token
    exchange) → `/smart/app`.
11. The SMART app shows the **full pre-populated order template with evidence** — it
    pulls the patient's FHIR data itself (`POST /smart/api/proposal`), generates a fresh
    proposal, and renders every recommended item with its rationale/evidence, unlike the
    CDS card's single trimmed resource. Deselect an item and edit a field (e.g. change
    the ceftriaxone dose from `1 g` to `2 g`) so the hand-back is visibly the
    clinician's set, not the AI's.
12. Click **Send to order entry** — `POST /smart/api/submit` in the default
    `handback` mode validates the edited selections against the catalog, stores them as
    a *refined proposal* keyed by encounter (falling back to patient), and writes an
    audit row with `kind="refinement"`. Nothing is POSTed to the EHR. The page confirms:
    *"Selections will appear as suggestions when you return to order entry."*
13. Go back to the chart tab, open the **Orders** tab and click **Re-check
    suggestions** — that re-fires `order-select` with the current drafts
    (`POST /api/hooks/order-select`).
14. The card that comes back is the **reviewed** one: summary
    `Reviewed in Order Assistant: N selected orders`, every suggestion
    `isRecommended: true`, the detail listing the clinician-edited values
    (`_(reviewed: dose: 2 g; route: IV; …)_`), the deselected item gone, and anything
    already in the draft tray still deduplicated away. The `create` action carries the
    edited values too — `"dosageInstruction": [{"text": "2 g IV q24h for 5 days"}]`.
15. Click **Accept** on the suggestions — each one files as an unsigned draft order and
    sends CDS Hooks feedback (`outcome: "accepted"`, with the suggestion uuid) back to
    the service.
16. Click **Sign** — the drafts flip to `active`. `patient-view` also reports a waiting
    reviewed set ("A set reviewed in the EpicVibe Order Assistant is waiting…") until
    the refinement's TTL expires.

Set `EPICVIBE_SMART_SUBMIT_MODE=fhir` to demo the older direct-write path instead: the
button reads **Send to EHR** and `POST /smart/api/submit` creates each resource against
the mock EHR via `POST /fhir/{ResourceType}` with the SMART session's access token. That
path works against the mock EHR and HAPI — not against Epic (see below).

Mention along the way:

- The **developer panel** (`GET /api/dev/history`, `GET /api/cds/status`) shows hook fire
  history and CDS discovery status — useful for showing "here's what actually got sent."
- **Reset** (`POST /api/reset`) reloads the mock EHR's fixture data and clears the OAuth
  stub, so the demo can be re-run from a clean slate.

### Why hand-back, not write-back

Inside Epic a SMART app has **no write channel for orders**. A direct
`ServiceRequest`/`MedicationRequest` create is refused (403/405) outside a CDS Hooks
interaction, and there is no SMART Web Messaging to hand an order to the ordering
context. `docs/spikes/fhir-order-writeback.md` records that spike and its addendum: CDS
Hooks *creates* are a **response capability**, not a REST one. The only thing that
actually files an order is an **accepted CDS card `create` suggestion**, which Epic turns
into an unsigned order for the clinician to sign.

So the SMART Order Assistant does not write. It hands its refined selections back to the
CDS service (`smart_submit_mode=handback`, the default), and the next `order-select` /
`order-sign` hook for that encounter emits those selections as `create` actions. The
clinician's round trip is: card → SMART app → refine → hand back → order entry → accept →
sign. The refined set is held in memory for `EPICVIBE_SMART_HANDBACK_TTL_SECONDS` and
takes precedence over the raw AI proposal for that encounter; the original AI proposal is
kept alongside it (and in the `refinement` audit row) so the clinician's edits can be
diffed against what was suggested.

### Direct dev entry point

Skip the chart click-through and launch the SMART app directly against a given patient:

```
http://localhost:8000/smart/dev/launch?patient=pat-okafor
```

(Requires `smart_dev_mode=true`, the default. `patient` accepts `pat-santos`,
`pat-okafor`, or `pat-brooks`.)

### Settings (`src/epicvibe/config.py`, prefix `EPICVIBE_`)

| Setting | Default | Purpose |
|---|---|---|
| `EPICVIBE_INFERENCE_PROVIDER` | `fake` | `demo` = fixed DM2 proposal, no key; `anthropic` = live model |
| `EPICVIBE_ANTHROPIC_API_KEY` | _(empty)_ | required only when provider is `anthropic` |
| `EPICVIBE_ANTHROPIC_MODEL` | `claude-haiku-4-5` | model id for the `anthropic` provider |
| `EPICVIBE_FHIR_PULL_ENABLED` | `true` | let the service fetch prefetch gaps directly from `fhirServer` |
| `EPICVIBE_FHIR_TIMEOUT_SECONDS` | `1.5` | per-resource timeout for that direct pull |
| `EPICVIBE_SMART_LAUNCH_URL` | `http://localhost:8000/smart/launch` | link target rendered on CDS cards |
| `EPICVIBE_SMART_REDIRECT_URI` | `http://localhost:8000/smart/callback` | OAuth redirect used in the authorize request |
| `EPICVIBE_SMART_CLIENT_ID` | `epicvibe-order-assistant` | SMART client id |
| `EPICVIBE_SMART_SCOPE` | `launch openid fhirUser patient/*.read patient/ServiceRequest.write patient/MedicationRequest.write` | requested OAuth scope |
| `EPICVIBE_SMART_SUBMIT_MODE` | `handback` | `handback` = store the refined set for the next CDS hook (the only Epic-viable write path); `fhir` = direct FHIR create against the EHR (mock EHR / HAPI only) |
| `EPICVIBE_SMART_HANDBACK_TTL_SECONDS` | `3600` | how long a handed-back refined set stays live for its encounter |
| `EPICVIBE_SMART_DEV_MODE` | `true` | enables `/smart/dev/launch` |
| `EPICVIBE_SMART_DEV_ISS` | `http://localhost:8100/fhir` | `iss` used by the dev launch shortcut |
| `EPICVIBE_SMART_ALLOWED_ISSUERS` | `["http://localhost:8100/fhir", "http://127.0.0.1:8100/fhir"]` | JSON list of FHIR base URLs `/smart/launch` will accept as `iss` |

`iss` arrives as a query parameter on `/smart/launch`, so it is attacker-controllable and
drives both an outbound discovery fetch (SSRF) and a browser redirect (open redirect).
Anything not on `EPICVIBE_SMART_ALLOWED_ISSUERS` is rejected with 400 (trailing slashes are
normalized), the discovery client does not follow redirects, and the discovered
`authorization_endpoint`/`token_endpoint` must share the issuer's origin (scheme, host and
port) or the launch is rejected. Point this at your EHR's FHIR base URL before launching
against anything other than the local mock EHR.

**Audit provenance.** Rows in the `proposals` audit table record which provider actually
produced the proposal, not the configured Anthropic model: `fake`, `demo`, or
`anthropic:<model id>` (e.g. `anthropic:claude-haiku-4-5`). A `demo` run is therefore never
mistaken for model output when you read the audit log back.

Mock EHR settings (`src/epicvibe/mockehr/settings.py`, prefix `MOCKEHR_`) — `MOCKEHR_PORT`,
`MOCKEHR_BASE_URL`, `MOCKEHR_CDS_BASE_URL`, `MOCKEHR_FIXTURES_DIR`,
`MOCKEHR_HOOK_TIMEOUT_SECONDS` — are independent of `EPICVIBE_*`; the mock EHR is a
standalone demo harness.

### Limitations

- **The mock EHR is not Epic.** It approximates the CDS Hooks + SMART launch contract
  well enough to demo the full loop, but it doesn't replicate Epic's orderable catalog,
  BPA presentation, or governance model.
- **Epic ignores order-detail fields in `create` actions.** Per the design spec
  (`docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md`), Epic always resolves
  our suggestion's code against its own orderables and composes order details from its
  own SmartSet/preference-list defaults — it does not honor the dose/route/timing we put
  in the draft resource. So inside Epic's native ordering UI, pre-population is limited
  to **selection + advisory text** (the card's `detail` and per-item rationale); the
  fully pre-populated order template only shows up on the **SMART app path** (steps 9–12
  above). That template's values do make it back into Epic's ordering context, but as the
  reviewed card's `detail` and `create` actions — via the hand-back, not via a write.
- **A SMART app cannot file orders in Epic.** Hence `smart_submit_mode=handback`; the
  `fhir` mode demonstrated against the mock EHR would fail in Epic. See "Why hand-back,
  not write-back" above and `docs/spikes/fhir-order-writeback.md`.
- **Real Epic requires the sandbox/production runbook.** See `docs/runbook-sandbox.md`
  for the CDS Hooks Sandbox, Meld, Epic FHIR sandbox, and customer non-prod steps.

---

## Scenario A: Offline / downtime — transcript → template → HL7v2 batch

### Processes

| Piece | Port | Start command |
|---|---|---|
| Mock integration engine | 2575 | `python -m epicvibe.downtime.mock_engine --port 2575 --inbox .downtime-inbox` |
| Downtime app | 8200 | `python -m epicvibe.downtime` |

The mock engine (`src/epicvibe/downtime/mock_engine.py`) is an asyncio MLLP server that
stands in for Epic Bridges/Rhapsody/Mirth: it accepts `ORM^O01` messages, writes each raw
message under `--inbox`, and replies with `ACK^O01`. `--reject-every N` makes it NACK
(`AE`) every Nth message — use it to demo the recovery path.

### Click-through

1. Open `http://localhost:8200`. The banner reads **"EHR OFFLINE — downtime mode"**.
2. **Load sample transcript** — pick one from `fixtures/downtime/transcripts/` (e.g.
   `chf-exacerbation-admission`).
3. Click **Generate orders** (`POST /api/generate`). The extractor (`fake` provider by
   default — deterministic keyword/regex matching, no API key) returns:
   - a **template selection** with confidence and alternatives (e.g. matches
     `chf-exacerbation-admission` — "Heart Failure Exacerbation - Admission" — with one
     or more alternative templates listed),
   - the **guideline** the template is derived from, printed under the template name
     (e.g. "ACC/AHA/HFSA 2022 Guideline for the Management of Heart Failure"),
   - **patient fields** with a provenance chip each (see below) and a red
     required-missing indicator on any required field with no value,
   - **orders grouped by category** (medication, lab, imaging, etc.) with dose/route/
     frequency parsed where the transcript stated them; deferred/low-confidence defaults
     start **deselected** rather than silently ordered.
4. Edit any field or toggle an order's checkbox, then **Save draft**
   (`POST /api/orders`, or a follow-up `PUT`-style update if editing an existing draft).
5. Click **Sign** (`POST /api/orders/{id}/sign`) — the order moves to `queued`.
6. Click **Preview HL7** (`GET /api/orders/{id}/hl7`) to show the `ORM^O01` before it's
   sent.
7. Toggle the banner to **"EHR ONLINE"** (client-side state only — enables the submit
   button; it does not change any server setting).
8. Click **Submit batch to EHR** (`POST /api/submit-batch`) — every `queued` order is
   sent over MLLP to the mock engine and gets an **ACK** (`AA`, filed) or **NACK**
   (`AE`, rejected) back, one per order.
9. **HL7 log** (`GET /api/orders/{id}/log`) shows the sent message and the ACK/NACK for
   that order.
10. Any order that comes back NACKed — or fails to reach the engine at all — lands on
    the **recovery worklist** (`GET /api/recovery`): a printable list of exactly what did
    not make it into Epic, so nothing is lost.
11. To demo NACKs deliberately, restart the mock engine with
    `python -m epicvibe.downtime.mock_engine --reject-every 3` and submit a batch of 3+
    orders.

### Field provenance: the three chips

Every filled field carries a `source` — `transcript`, `default` or `none` — stamped by
`validate_filled()` in `src/epicvibe/downtime/engine.py`, after the provider has
answered and regardless of which provider answered. It is the one place the decision is
made, so the `fake`, `ollama` and `anthropic` paths cannot disagree about it.

| Chip | `source` | Means | Where the value came from |
|---|---|---|---|
| green, quoting the transcript | `transcript` | the clinician said it | the model, backed by a verbatim quote in `evidence` (hover to read it) |
| grey, dashed, "template default" | `default` | nobody said it | the template's `defaults` for that order — a guideline-derived value written by a human. Hover for the order's `guideline_note` |
| red, "not in transcript" | `none` | a gap | nowhere. Required gaps are also listed in `unresolved` and outline the input in red |

Rules the post-validation enforces:

- A value with a quote is `transcript`. The quote is checked against the actual
  transcript, case- and whitespace-insensitively; if it is not there the value is
  **kept** (the clinician has to see what the model produced in order to reject it) and
  a `evidence not found verbatim` warning is added to the warnings banner.
- A field the model left empty is back-filled from the template `defaults` and labelled
  `default`. The fill prompt now tells the model this happens automatically, so it must
  leave unsupported fields empty rather than guessing them.
- **Patient fields never take a default.** There is no guideline-sanctioned guess for
  someone's name, DOB or allergies — those stay `none` and show red.
- Editing a field in the UI clears its chip: a value the clinician typed is neither a
  transcript quote nor a template default.

The guideline itself lives on the template JSON (`guideline: {name, organization, year,
url}`) with an optional per-order `guideline_note`; the six fixtures cite IDSA/ATS 2019
(CAP), ACC/AHA 2021 (chest pain), Surviving Sepsis Campaign 2021, ADA Standards of Care
2025 (DKA and new T2DM) and ACC/AHA/HFSA 2022 (heart failure). This is the answer to
"did the AI make that dose up?": a grey chip means the value came from a
governance-approved order set, not from a model.

### Offline-strict mode (`src/epicvibe/downtime/offline.py`)

`GET /api/offline` returns a per-item checklist of everything in the deployment that
could reach off-box, and `/api/status` carries the summary (`offline: {strict,
all_local, not_local}`) that drives the **LOCAL ONLY** badge in the status bar.

| Check | Passes when |
|---|---|
| `provider_local` | provider is `fake` or `ollama` — and for `ollama`, `ollama_base_url` points at a loopback or private address |
| `whisper_installed` | `faster-whisper` is importable |
| `whisper_model_cached` | the Whisper weights are already on disk, so no download is needed |
| `engine_reachable` | the configured MLLP `engine_host:engine_port` is a loopback/private address **and** accepts a TCP connection (0.5 s timeout) |

| Var | Default | Purpose |
|---|---|---|
| `EPICVIBE_DOWNTIME_OFFLINE_STRICT` | `false` | refuse to start unless every check above passes |

With `STRICT=true`, `create_app()` raises a `RuntimeError` at startup listing exactly
which items are not local, before it builds anything else — a loud failure on the
projector beats a quiet egress at the bedside. The status bar then reads
**LOCAL ONLY · STRICT**; without strict it reads **LOCAL ONLY** when everything passes,
and names the failing items in muted text when it does not. Hover the badge for the
full checklist either way.

### Audio input (local Whisper, no network)

Step 2 can start from audio instead of pasted text. Transcription runs entirely on the
box — `faster-whisper` (CTranslate2, CPU int8), which is the point: a declared downtime
is exactly when a cloud STT vendor is unavailable, and the audio is ambient PHI.

Install the optional extra (once):

```bash
pip install -e ".[dev,audio]"
```

Without it the app still starts; the transcript panel just shows a muted install hint
instead of the audio controls, and the endpoints answer `501`.

In the **Ambient transcript** panel:

- **● Record** — captures from the mic via `MediaRecorder` (webm/opus) with an elapsed
  timer; **■ Stop** posts the clip and fills the textarea.
- **Upload audio** — any wav/webm/ogg/mp3/m4a/flac file.
- **Transcribe sample audio…** — `fixtures/downtime/audio/ed-cap-admission-excerpt.wav`,
  an 82-second synthesized read of the CAP encounter, so the demo works with no mic.

The result replaces the textarea contents (with a confirm if you have already typed
something) and reports model, elapsed seconds, audio duration and segment count.
Then continue at step 3 — **Generate orders** — as usual.

| Endpoint | Purpose |
|---|---|
| `POST /api/transcribe` | Raw audio bytes in the body (`Content-Type: audio/wav`, `audio/webm`, …) → `{text, segments, language, duration_s, model, elapsed_s}`. No multipart, so no extra dependency. |
| `GET /api/transcribe/status` | `enabled` / `installed` / `model` / `model_cached` / sample names. |
| `GET /api/transcripts/audio` | Lists the fixture audio files. |
| `POST /api/transcribe/sample/{name}` | Transcribes one fixture file server-side. |

Settings live in `src/epicvibe/downtime/transcribe.py` under prefix
`EPICVIBE_DOWNTIME_WHISPER_`:

| Var | Default | Notes |
|---|---|---|
| `ENABLED` | `true` | Set false to hide the controls and return `501`. |
| `MODEL` | `small` | Any faster-whisper model id, or a path to converted weights. |
| `MODEL_DIR` | unset | Pre-seeded weights directory — see offline note below. |
| `LANGUAGE` | `en` | Skips language detection. |
| `BEAM_SIZE` | `1` | Greedy; raise for accuracy at a CPU cost. |
| `VAD_FILTER` | `true` | Drops silence before decoding. |

**Weights and truly-offline boxes.** On first use the `small` model (~480 MB, int8
CTranslate2) downloads from Hugging Face into
`~/.cache/huggingface/hub/models--Systran--faster-whisper-small`
(`%USERPROFILE%\.cache\huggingface\hub\...` on Windows). That is the only network
call in the whole feature, and it never happens again. For a machine that will never
have internet, copy that model directory onto the box and point
`EPICVIBE_DOWNTIME_WHISPER_MODEL_DIR` at it: if the directory contains `model.bin` it is
used as the model itself, otherwise it is used as the download root. `GET
/api/transcribe/status` reports `model_cached` so you can confirm before the demo.

Measured on this CPU: the 82-second sample transcribes in **~12-15 s** (roughly 6x
realtime) with `small`/int8/beam 1. Quality on synthesized speech is good enough for the
keyword extractor — "pneumonia", "azithromycin", "chest x-ray", "sputum" and the patient
name all come through, and `/api/generate` still selects `ed-cap-admission`. Drug names
are the weak spot: *ceftriaxone* comes back as "seftriaxone". That is exactly why the
filled order set is reviewed and signed by a human before anything becomes HL7.

### Settings (`src/epicvibe/downtime/config.py`, prefix `EPICVIBE_DOWNTIME_`)

| Setting | Default | Purpose |
|---|---|---|
| `EPICVIBE_DOWNTIME_PROVIDER` | `fake` | `fake` = deterministic keyword extractor, no key; `anthropic` = live model |
| `EPICVIBE_DOWNTIME_ANTHROPIC_API_KEY` | _(empty)_ | falls back to `ANTHROPIC_API_KEY`, then `EPICVIBE_ANTHROPIC_API_KEY` |
| `EPICVIBE_DOWNTIME_MODEL` | `claude-opus-5` | model used for template selection + filling when `provider=anthropic` |
| `EPICVIBE_DOWNTIME_TEMPLATES_DIR` | `fixtures/downtime/templates` | offline order-template library |
| `EPICVIBE_DOWNTIME_TRANSCRIPTS_DIR` | `fixtures/downtime/transcripts` | sample ambient transcripts shown in "Load sample" |
| `EPICVIBE_DOWNTIME_DB_PATH` | `downtime.db` | durable captured-order store (SQLite) |
| `EPICVIBE_DOWNTIME_ENGINE_HOST` / `_ENGINE_PORT` | `127.0.0.1` / `2575` | where the downtime app connects for MLLP submission |
| `EPICVIBE_DOWNTIME_SENDING_APPLICATION` / `_SENDING_FACILITY` | `EPICVIBE` / `DOWNTIME` | HL7 MSH-3/MSH-4 |
| `EPICVIBE_DOWNTIME_RECEIVING_APPLICATION` / `_RECEIVING_FACILITY` | `EPIC` / `BRIDGES` | HL7 MSH-5/MSH-6 |
| `EPICVIBE_DOWNTIME_HOST` / `EPICVIBE_DOWNTIME_PORT` | `127.0.0.1` / `8200` | bind address read by `python -m epicvibe.downtime` |

For a live-model demo of the extraction step, set `EPICVIBE_DOWNTIME_PROVIDER=anthropic`
and either `EPICVIBE_DOWNTIME_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY`.

### HL7 message anatomy (`src/epicvibe/downtime/hl7.py: build_orm`)

Each signed order set becomes one `ORM^O01` message (`\r`-separated segments):

| Segment | Contents |
|---|---|
| `MSH` | message header — sending/receiving application & facility, timestamp, message type, control id (MSH-10) |
| `PID` | patient identity — real MRN when known (PID-3 with the receiving facility as assigning authority), otherwise a minted `DT-xxxxxxxx` id with assigning authority `EPICVIBE_DOWNTIME` and identifier type `DTID` |
| `PV1` | encounter class (mapped from the template's `setting`: ED→`E`, inpatient→`I`, ambulatory→`O`), location, ordering provider |
| `AL1` | one segment per parsed allergy |
| `ORC` | per order — order control `NW` (new), placer order number, ordering provider, indication |
| `OBR` | the order itself — universal service id (code/display/system), priority, requested date/time, reason |
| `RXO` | medications only — give code, dose, units, administration instructions |
| `RXR` | medications only — route |
| `NTE` | free-text notes — indication, the transcript evidence quote that justified the field, and the capture rationale |

The mock engine ACKs with `MSA-1 = AA` (accept) or `AE` (reject, when `--reject-every`
fires); `hl7.parse_ack` reads `MSA-2` back as the control id being acknowledged.

**Reconciling to a real MRN**: once Epic is back and a downtime patient is matched to a
real chart, `POST /api/orders/{id}/reconcile` (body `{"mrn": "<real MRN>"}`) records the
match and flips the order's status to `reconciled` — this is bookkeeping on our side
only; it does not resend the HL7 message (that already went out with whatever identifier
was known at submit time).

### Limitations

- **The mock engine is not Bridges.** It proves the MLLP/ORM/ACK mechanics work
  end-to-end, but a real deployment needs the customer to actually stand up an inbound
  ORM interface on their integration engine — feasibility, timeline, and filing behavior
  (pended vs. cosign queue) are open questions (design spec §7, "Bridges interface
  feasibility").
- **No durable hosting story yet.** The demo runs the downtime app and its SQLite store
  on a laptop; a real downtime tool needs to be reachable through whatever outage it's
  meant to survive (network-wide vs. Epic-application-only — also spec §7 open question
  1), which the demo does not address.
- **No real provider auth.** `signed_by` here is free text; production needs the
  customer's answer to spec §7 open question 2 (SSO/AD vs. managed break-glass roster).
