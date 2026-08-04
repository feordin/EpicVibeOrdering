# AI-Assisted Order Set Selection for Epic — Design

**Date:** 2026-08-04
**Status:** Draft for review
**Supersedes:** guidance in `epic-order-sets-summary.md` (written for the offline-downtime
use case; several of its assumptions are corrected here)

---

## 1. Problem and Goal

Providers at our customer's ambulatory clinics assemble orders by hunting through Epic's
order-set catalog, picking the right SmartSet, and choosing the right component items and
variants for the patient in front of them. The goal is an AI service that does the hunting
and choosing for them — grounded in the institution's own governance-approved order sets —
while the provider **stays entirely inside Epic's native ordering UI**, reviews, edits,
and signs exactly as they do today.

A real customer is engaged. Development starts against public sandboxes; the customer's
non-production Epic environment is the integration milestone. The customer provides:

- An **order-set extract** from their Epic (already in hand).
- An **approved PHI-safe AI inference endpoint** (we start with an off-the-shelf
  BAA-capable endpoint; the AI layer is pluggable by configuration).

## 2. Verified Integration Constraints (what Epic actually supports)

These facts, confirmed from Epic's public CDS Hooks documentation (fhir.epic.com) in
August 2026, bound the design:

1. Epic supports exactly three CDS Hooks: `patient-view`, `order-select`, `order-sign`,
   implemented on its OurPractice Advisory (OPA) framework.
2. Suggestion cards **can create real unsigned orders** when accepted: single medication
   orders (MedicationRequest), single procedure/lab/imaging orders (ServiceRequest), and
   order-set/SmartSet items via `urn:com.epic.cdshooks.action.code.system.orderset-item`
   and preference-list items via
   `urn:com.epic.cdshooks.action.code.system.preference-list-item`. One action per
   suggestion; `selectionBehavior: any`; `isRecommended` pre-selects.
3. **Epic ignores order details in the payload.** All order details (dose, route,
   frequency, priority, instructions) come from the Epic-side defaults of the targeted
   orderable/preference-list/SmartSet entry. We choose *which* orderable, never its
   field values.
4. No custom CDS Hooks context fields; prefetch templates are configured by the
   customer's analysts in the OPA record. Every hook call carries a short-lived
   `fhirAuthorization` token for FHIR reads.
5. SMART Web Messaging is not supported by Epic; an embedded SMART app has no channel
   into the order scratchpad. systemActions support is limited to annotating existing
   unsigned ServiceRequests (`ServiceRequest.Update (Unsigned Order)`, Feb 2024+).
6. There is **no general-purpose FHIR order create** at Epic. FHIR order write-back
   exists only as CDS Hooks unsigned-order creation (clinician signs in Epic).
7. CDS Hooks cannot be tested against Epic's public sandbox; real hook testing requires
   a customer (non-prod) environment.
8. No Epic transcript/intent API exists for third parties, and Epic ships its own
   ambient ordering ("Art," GA ~Feb 2026). Ambient input is **deferred but designed
   for** (Section 8).

**Design consequence:** the AI's job is *selection* — the right order set, the right
items, the most specific pre-built variant. Epic's own build does the composing. This is
also the safety story: the AI structurally cannot invent a dose; every suggestion is an
institutionally approved orderable.

## 3. Architecture Overview

A Python backend service, no provider-facing UI in v1.

```
                                      ┌──────────────────────────────┐
Epic (customer)                       │  Our Backend (Python)        │
┌─────────────────┐   order-select    │  ┌────────────────────────┐  │
│ Provider in     │   patient-view    │  │ CDS Hooks Service      │  │
│ Hyperdrive      │──── POST ────────▶│  │ (FastAPI: discovery +  │  │
│                 │◀─── cards ────────│  │  3 hook handlers)      │  │
│ OPA advisory →  │                   │  └───────┬────────────────┘  │
│ unsigned orders │   FHIR reads      │          ▼                   │
└─────────────────┘◀─────────────────▶│  ┌────────────────────────┐  │
                                      │  │ Proposal Engine        │  │
Order-set extract                     │  │ patient ctx + catalog  │  │
(customer files) ────── loader ──────▶│  │ → detailed proposals   │  │
                                      │  └───────┬────────────────┘  │
                                      │          ▼                   │
                                      │  ┌────────────────────────┐  │
                                      │  │ Emitters (channels)    │  │
                                      │  │ v1: CDS suggestions    │  │
                                      │  │ later: richer pre-fill │  │
                                      │  └────────────────────────┘  │
                                      │  Pluggable inference ────────┼──▶ AI endpoint
                                      │  Audit store (SQLite→PG)     │   (config-selected)
                                      └──────────────────────────────┘
```

### Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `catalog` | Parse extract files → normalized catalog; query API | extract format |
| `cds_service` | FastAPI endpoints, Epic JWT validation, context/prefetch parsing, card rendering | catalog, proposal_engine |
| `proposal_engine` | Patient context + catalog → validated, fully detailed order proposals | catalog, inference |
| `inference` | Pluggable LLM endpoint interface + implementations | config |
| `emitters` | Validated proposal → output channel (v1: CDS suggestion cards) | proposal_engine |
| `audit` | Record proposals, decisions, feedback; eval export | storage |

Each module is independently testable. The proposal engine runs headless for evals.

## 4. Tech Stack

- **Python 3.12+**, **FastAPI** for the CDS Hooks service (the spec is plain
  JSON-over-HTTP; no framework ecosystem exists or is needed).
- **`fhir.resources`** — typed, pydantic-validated R4 resource models for parsing
  prefetch data and constructing suggestion-action resource stubs.
- **`fhirclient`** (SMART Health IT client-py) — SMART launch OAuth when a launched app
  or standalone reads are needed. Most runtime FHIR access uses the per-hook
  `fhirAuthorization` token directly (`httpx`). SMART Backend Services (asymmetric JWT)
  via `pyjwt` if system-level access is later required.
- **pydantic** — the proposal schema, used both as the JSON schema given to the LLM for
  structured output and as the validator of its response.
- **pandas** — catalog loader (exploratory parsing of Clarity/Reporting Workbench TSV
  extracts) and the eval harness.
- **Inference** — `InferenceProvider` interface (endpoint, auth, model from config).
  First implementation: off-the-shelf BAA-capable endpoint (Anthropic API or AWS
  Bedrock). Customer's approved endpoint is a drop-in second implementation.
- **Storage** — catalog: JSON files loaded at startup; proposal cache: in-memory,
  per-encounter, short TTL; audit: SQLite for prototype → PostgreSQL for production.

Rationale for Python over TS/.NET/Java: the two workloads where stack choice genuinely
matters here are (a) exploratory parsing of a messy, site-customized extract format and
(b) the eval harness — both pandas territory — plus first-class AI SDK and pydantic
structured-output ergonomics. Epic offers no SDK in any language (integration is plain
HTTPS/JSON), and this app's FHIR footprint is light, so HAPI/Firely's deeper FHIR
tooling isn't exercised. A future companion UI would be TypeScript regardless.

## 5. Data Flow

### Offline (per extract refresh)

Customer extract files → pandas loader → normalized catalog:
order sets → groups → orderable items → variants. Every node retains its Epic
identifiers (SmartSet ID, preference-list item ID, code + code system) so suggestions
can target it, plus its Epic-side default details (dose/route/frequency) so the AI knows
what accepting a variant produces. Loader tolerance rules: unknown columns warn, never
fail; unrecognized tables ignored; extract version metadata (Epic version, timestamp,
site) recorded for drift detection.

### Runtime (per encounter)

1. **`patient-view`** at chart open: handler reads prefetched FHIR bundle from the
   request, enqueues a background inference job, returns immediately (see §6).
2. **Background job:** supplemental FHIR reads if needed (using the hook's
   `fhirAuthorization` token immediately — it is short-lived), build compact patient
   summary, deterministic pre-filter of the catalog (diagnosis→order-set mapping,
   code/keyword match) to a shortlist, LLM call with patient summary + shortlist +
   strict pydantic JSON schema.
3. **Proposal** returned: selected order set(s), included/excluded items, chosen
   variants, computed parameter recommendations (advisory display only in v1), per-item
   rationale. Deterministic validation drops anything not in the catalog (logged,
   hard rule). Validated proposal cached per encounter.
4. **`order-select`** while ordering: cache read + deterministic match of in-progress
   draft orders against the proposal → suggestion cards (companion items, better
   variants) in tens of milliseconds. Card markdown shows the AI's recommended values
   ("suggested: metformin 500 mg BID") and rationale even though Epic composes from its
   own defaults.
5. Provider accepts/ignores → Epic posts to our CDS Hooks feedback endpoint → audit
   store records proposal, decision, model + version, timestamps.
6. **`order-sign`**: cache-only completeness check ("protocol includes urine
   microalbumin — not present"); cards only, never blocking.

### Grounding guarantees (two layers)

- **Retrieval:** the LLM only ever sees catalog slices (the shortlist).
- **Validation:** any proposed item not resolvable to a catalog node is dropped and
  logged. Catalog-grounding violations are a first-class quality metric.

## 6. Latency Architecture

Epic hooks expect responses in ~2 seconds. **Rule: the LLM is never in the synchronous
hook path.** Hook handlers do only cache lookups and deterministic work.

- `patient-view` = warm-up trigger: enqueue job, respond in milliseconds.
- Background inference takes as long as it takes (target p50 1–3s via fast model,
  prompt caching of the static prefix, small shortlist prompt, bounded output schema);
  the cache is warm before the provider finishes reviewing the chart.
- `order-select` / `order-sign` = cache reads.
- **No cache yet** (provider jumps straight to orders): return no cards this time,
  enqueue; subsequent hook firings in the same visit serve from cache. Degraded, never
  delayed.
- **Context drift** (new dx, changed meds mid-visit): handlers cheaply diff current hook
  context against the cached proposal's assumptions; if material, suppress and
  re-enqueue. Per-encounter cache, short TTL.
- **Backstop:** internal handler deadline (~800 ms); on any internal stall return an
  empty valid response. A missing card is invisible; a timeout invites the site to
  disable the service.
- Site alignment needed: OPA configuration must fire `patient-view` at chart open for
  the warm-up to work (Epic advises sites against over-firing it; needs explicit
  agreement with customer analysts), plus prefetch template setup.

## 7. Error Handling

Governing principle: **fail silent, never block ordering.**

- Service error/timeout → Epic shows no card; provider workflow unaffected.
- LLM failure → no cards (never partial/unvalidated suggestions); log.
- Catalog validation failure → drop item, log prominently, continue with valid
  remainder.
- Missing/malformed prefetch → propose from what exists with lowered confidence, or
  stay silent below a confidence floor.
- Auth: validate Epic's JWT (jku/JWKS) on every hook call; failure → 401, no
  processing.
- PHI hygiene: no PHI in application logs; patient identifiers only in the audit store;
  minimum-necessary context sent to the inference endpoint.

## 8. Deferred: Ambient Input (designed-for)

No Epic API exposes in-visit transcript or intent to third parties, and CDS Hooks
context is not extensible. Ambient integration is therefore out of v1 scope. The
Proposal Engine, however, accepts a generic `intent_signals` input alongside patient
context. Future sources plug in without engine rework:

1. Ambient vendor API partnership (Suki has a public developer platform; Dragon Copilot
   has a partner program; Abridge is partner-gated).
2. Own capture outside Epic (provider device mic → transcription → intent extraction).
3. Post-hoc: ambient-generated note read via FHIR DocumentReference after signing
   (useful for next-touchpoint suggestions, not in-visit).

## 9. Parallel Track: Richer Pre-fill

The engine computes fully detailed proposals from day one; channels determine how much
detail reaches Epic. Staged by cost:

| Stage | What | Cost / dependency |
|---|---|---|
| 1 (in v1) | Advisory values in card markdown + variant targeting via preference-list granularity | None |
| 2 | Audit customer's ambulatory order sets for too-coarse defaults; targeted Epic build work to add variants where selection needs resolution | Customer analyst time |
| 3 | Investigate `ServiceRequest.Update (Unsigned Order)` systemAction with customer's Epic TS: which fields can it touch? | Login-gated docs + TS question; free |
| 4 | Vendor Services membership → Interoperability Request for pended-order APIs, or per-site HL7v2 ORM interface | $, customer sponsorship, months |
| 5 | Epic Workshop co-development (the Abridge path) | Invitation-only |

Each stage that lands is a new emitter; engine unchanged.

## 10. Testing and Evaluation

1. **Unit/contract:** pydantic schemas across all boundaries; golden-file loader tests
   against real (de-identified) extract samples; fixture request/response tests per
   hook.
2. **Integration:** CDS Hooks Sandbox (sandbox.cds-hooks.org) fires simulated hooks
   end-to-end; SMART launcher + Epic public FHIR sandbox validate consumed payload
   shapes. Acceptance milestone: first real card in the customer's non-prod Epic.
3. **AI evals (continuous):** synthetic ambulatory patients (Synthea + hand-built cases
   matched to the customer's actual order sets) run headless through the engine on
   every model/prompt change. Metrics:
   - **Catalog grounding — 100%, hard gate.**
   - **Recall** of expected components per clinical scenario.
   - **Precision** — no spurious components.
   In production, audit-store accept/override rates become the ongoing eval.

## 11. Rollout Path

1. **Prototype:** catalog loader on the real extract + proposal engine + evals + CDS
   service against public sandboxes. Demoable end-to-end without Epic.
2. **Customer non-prod:** client ID registration (free, fhir.epic.com), analyst-built
   OPA record + prefetch templates + endpoint allowlisting; real hook firing; latency
   and card-rendering validation.
3. **Pilot:** small provider group, interruptive-vs-passive presentation tuned with the
   customer, audit-driven quality review.
4. **Program housekeeping when live:** Connection Hub listing (~$500/yr); Vendor
   Services only if/when Stage 4 of the pre-fill track justifies it.

## 12. Out of Scope (v1)

- Any provider-facing UI of our own (a read-only SMART companion for rationale display
  is a possible later addition).
- AI-computed values written into Epic order fields (see §9 for the staged path).
- Ambient capture or transcript processing (§8).
- Inpatient and ED settings (ambulatory first; hooks and architecture carry over).
- Fully signed programmatic order creation (not publicly possible at Epic; clinician
  always signs).

## 13. Open Questions (tracked, non-blocking)

1. Exact format/completeness of the customer's order-set extract (loader is built
   against the real files; tolerance rules in §5).
2. What fields the unsigned-order Update systemAction can modify (Stage 3 of §9 —
   customer Epic TS).
3. Customer's OPA configuration appetite: `patient-view` firing frequency, passive vs
   interruptive presentation.
4. Which ambient vendor (if any) the customer uses today — determines the Section 8
   integration path when it activates.
5. Whether the customer's approved AI endpoint supports JSON-schema-constrained output
   natively (affects the inference adapter's validation/retry strategy).
