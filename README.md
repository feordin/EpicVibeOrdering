# EpicVibe Ordering

EpicVibe is an AI-assisted order-set assistant with two complementary modes. **Online**,
it's a CDS Hooks service that suggests order-set items to clinicians inside Epic's native
ordering UI and offers a SMART on FHIR app for the fully pre-populated order template
(Epic ignores order-detail fields on CDS `create` actions and always uses its own
SmartSet/preference-list defaults, so the SMART app is where full pre-population lives).
**Offline**, during an Epic downtime, it's a standalone capture UI that turns an ambient
transcript into a reviewed, signed order set and writes it back as an HL7v2 `ORM^O01`
batch once Epic returns. Both modes ground proposals in the customer's own
governance-approved order-set catalog — no invented order details. See
`docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md` for the online design and
`docs/superpowers/specs/2026-08-04-downtime-ordering-design.md` for the downtime design.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` as needed — defaults use the `fake` inference provider and an in-repo sample
catalog, so the service runs out of the box with no external API key.

## Run tests

```bash
python -m pytest
```

## Run the server

```bash
uvicorn epicvibe.cds.app:create_app --factory --port 8000
```

Discovery endpoint: `GET http://localhost:8000/cds-services` — lists the three CDS
services (`epicvibe-patient-view`, `epicvibe-order-select`, `epicvibe-order-sign`).

## Run evals

```bash
python -m evals.run
```

Scores generated proposals against `evals/scenarios/*.json` expectations. Runs against the
`demo` provider by default (`--provider anthropic` / `EPICVIBE_INFERENCE_PROVIDER` to
override). The run exits non-zero unless every scenario is grounded and free of forbidden
items, mean recall is at least 0.80, and no single scenario falls below 0.50 recall (see
`docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md` §10).

## Demo

For a practical, fully-offline demo of both scenarios (no API key required — the `demo`
CDS provider and the downtime `fake` extractor), see `docs/runbook-demo.md`. For a
~20-minute presenter's script covering both scenarios live, see
`docs/demo-walkthrough.md`. Quick start:

```bash
python -m uvicorn epicvibe.cds.app:create_app --factory --port 8000      # CDS service      :8000
python -m epicvibe.mockehr                                               # mock EHR         :8100
python -m epicvibe.downtime.mock_engine --port 2575 --inbox .downtime-inbox   # mock HL7 engine  :2575
python -m epicvibe.downtime                                              # downtime app     :8200
```

## Next steps

For running the service end-to-end against a real interactive CDS Hooks client, an Epic
FHIR sandbox, or a customer's non-production Epic, see `docs/runbook-sandbox.md`.

For a containerised second demo that swaps our in-repo mock EHR for third-party
reference implementations (HAPI FHIR R4, the SMART App Launcher v2, and the CDS Hooks
Sandbox) via `docker compose up` in `demo/reference-sandbox/`, see
`docs/runbook-reference-sandbox.md`.
