# EpicVibe Ordering

EpicVibe is a CDS Hooks service that suggests order-set items to clinicians inside Epic's
native ordering UI. It reads a customer's own governance-approved order-set catalog,
grounds an AI-generated proposal in that catalog (no invented order details — Epic
ignores payload order details anyway and always uses its own SmartSet/preference-list
defaults), and renders the result as CDS Hooks cards on `patient-view`, `order-select`,
and `order-sign`. See `docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md` for
the full design (architecture, latency model, error handling, rollout path).

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

Scores generated proposals against `evals/scenarios/*.json` expectations; grounding is a
hard gate (see `docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md` §10).

## Next steps

For running the service end-to-end against a real interactive CDS Hooks client, an Epic
FHIR sandbox, or a customer's non-production Epic, see `docs/runbook-sandbox.md`.
