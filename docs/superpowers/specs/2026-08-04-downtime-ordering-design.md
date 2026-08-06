# Downtime Ordering Support — Design (Phase 2)

**Date:** 2026-08-04
**Status:** Draft — deliberately lean; emergency capability, not the primary product.
**Depends on:** `2026-08-04-epic-ai-ordering-design.md` (the "core spec"). The shared
core (catalog pipeline, proposal engine, inference layer, audit) is built first under
that spec; this subsystem consumes it.

## 1. Use Case

Epic goes down. Providers need to keep ordering electronically instead of falling back
to paper. Our app — which already holds the institution's order-set catalog — provides a
capture UI: identify the patient, pick the order set, complete the order details, sign.
Captured orders are stored durably and, when Epic returns, are written back with as
little re-keying as possible. Eliminates paper and (where the interface exists) manual
re-entry.

This is an **emergency scenario**: design for reliability and simplicity, not feature
depth.

## 2. What Changes vs. the Core Spec

| Core spec premise | Downtime reality |
|---|---|
| No provider-facing UI | Capture UI required (no Epic UI exists during downtime) |
| Epic composes order details | **We** compose full order details (catalog variant defaults pre-fill; provider edits freely) |
| Patient context from FHIR/hooks | Manual patient identification (name/MRN/DOB), optionally read from Epic BCA screens |
| Auth via Epic launch/JWT | Own provider auth (see Open Questions) |
| Storage = audit only | Captured orders are durable clinical records in flight (PostgreSQL, backed up) |

## 3. What Is Reused

- **Catalog** — the extract→loader→catalog pipeline IS the offline order-set cache.
  The downtime app reads the same normalized catalog; variant defaults pre-fill order
  forms.
- **Proposal engine + inference** — optional AI assist in the capture UI ("58yo with
  new T2DM" → proposed order set + items, fully detailed). Degrades to manual catalog
  browsing if the AI endpoint is unreachable. The engine's full-detail proposals are
  finally applied directly, since we own the composer here.
- **Audit store** — same provenance discipline: who ordered what, when, AI involvement,
  edits.

## 4. Write-Back (decided, pending spike result)

Public documentation indicates FHIR cannot write orders into Epic outside CDS Hooks
(only CDS-Hooks-scoped unsigned-order creates are listed on open.epic). We do NOT take
this on faith: **a spike verifies it empirically before any Phase 2 build.** CDS-card
staging is rejected for downtime backlogs only (long lists don't fit card UX); CDS
suggestion cards remain the core write mechanism of the live AI-assisted workflow
(core spec) — unaffected by this section.

**Spike (runs early, during Phase 1):** register a free app on fhir.epic.com; inspect
the client-ID API picker for any standalone `ServiceRequest.Create` /
`MedicationRequest.Create` (the picker is the source of truth for what is invocable);
attempt order creates against the sandbox; ask the customer's Epic TS whether any
site-specific FHIR order write exists for their contract. Record findings in
`docs/spikes/fhir-order-writeback.md`.

**Spike result (2026-08-06, see `docs/spikes/fhir-order-writeback.md`): FHIR order
write-back is NOT viable.** Authenticated sandbox attempts returned 403 for
ServiceRequest.Create (context-bound to CDS Hooks / niche radiotherapy API) and 405
for MedicationRequest.Create (no such operation). Channel priority is settled:

1. **Primary: HL7v2 ORM via Epic Bridges** — per-site interface, customer Epic team
   builds their side; we export captured orders as ORM^O01 filed per their build
   (e.g., pended for cosign). Long-lead item: start the interface conversation with
   the customer now.
2. **Fallback (day one, always built): recovery worklist** — per-patient structured
   list of captured orders (screen + print/export) for assisted manual entry.
3. **Optional: documentation note** — `DocumentReference.Create` (supported by Epic
   over FHIR) filing a downtime-orders summary note to the chart; add that API to the
   app registration when built.
4. Residual TS question (low probability): any site-specific order-write mechanism in
   the customer's contract beyond the public surface.

Reconciliation: captured orders carry manually-entered patient identifiers; at
recovery, a reconciliation step matches them to real Epic patients (search via FHIR
once Epic is back) before export.

## 5. Components (new in this phase)

| Component | Responsibility |
|---|---|
| `capture-ui` | Web UI (TypeScript/React): downtime session, patient identification, catalog browse + AI assist, order form pre-filled from variant defaults, provider sign-off |
| `downtime-auth` | Provider authentication independent of Epic (see Open Questions) |
| `order-store` | Durable captured-order records (PostgreSQL): draft → signed → reconciled → exported/entered lifecycle |
| `recovery` | Reconciliation UI + worklist + HL7v2 ORM exporter |

## 6. Mode Awareness

The service knows whether Epic is up (config toggle set by admins during a declared
downtime; optionally a health probe against the FHIR endpoint). Downtime mode gates the
capture UI; recovery mode surfaces the worklist/reconciliation flow. The CDS Hooks
service (core spec) is simply idle while Epic is down.

## 7. Open Questions (blocked on customer input)

1. **Hosting/reachability:** must the app be reachable during network-wide outages
   (on-prem/HA requirement) or only Epic-application outages (cloud fine)? What do the
   customer's declared-downtime procedures assume?
2. **Provider auth:** hospital SSO/AD integration, or a managed local roster with
   break-glass accounts? What does the customer's downtime policy require for order
   signing authority?
3. **Bridges interface feasibility:** will the customer stand up an inbound ORM
   interface for downtime recovery? Timeline and filing behavior (pended vs cosign
   queue)?
4. Does the customer's downtime workflow require label/requisition printing (labs need
   specimen labels when the lab system is also degraded)?
5. Retention/legal: how long do captured downtime orders persist after reconciliation?

## 8. Out of Scope

- Offline-first client (service-worker/local-storage ordering with no server) — the
  server is assumed reachable; revisit only if Open Question 1 says otherwise.
- Downtime documentation (notes, results viewing) — orders only. BCA covers read access.
- Automatic Epic-down detection triggering mode switch without human confirmation.

## 9. Sequencing

Phase 1 (current plan) builds the shared core + CDS Hooks service, and runs the FHIR
write-back spike (§4) alongside it. Phase 2 implements this spec once Open Questions
1–3 have customer answers and the spike result is recorded; an implementation plan is
written then. The only Phase-1 change made for Phase 2: none required — the catalog and
engine are already shared modules.
