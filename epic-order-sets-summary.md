# Epic Order Set Extraction — Conversation Summary

Consolidated from two chats, both titled "Offline hospital ordering with Epic order sets"
(May 26–27, 2026 and May 27, 2026).

---

## The Problem

Enable electronic ordering at a hospital during Epic downtime.

- Today: Epic goes down → providers fall back to paper forms → orders are manually
  re-keyed into Epic once it returns.
- Goal: dump order sets from Epic to files; an offline app renders them, captures
  orders electronically, and supports easy import back into Epic.
- Eliminates both paper and manual re-entry.

---

## Key Finding: Epic Does Not Expose Order Sets via FHIR

`PlanDefinition` and `ActivityDefinition` — FHIR's natural fit for order sets — are
absent from Epic's published FHIR resource list at fhir.epic.com. Epic's FHIR program
targets patient-scoped clinical data for USCDI/Cures compliance, not knowledge or
definitional artifacts.

The FHIR Clinical Reasoning module (`PlanDefinition`, `ActivityDefinition`, `Library`,
`RequestOrchestration`, and the `$apply` operation) is not implemented by Epic. The
canonical "pull a PlanDefinition, render it client-side, `$apply` it back" pattern
does not work against Epic today.

Underlying data model: order sets live in Chronicles as master files. Relevant Clarity
table family is `ORDER_SET`, `ORDER_SMARTSET`, `ORDER_SINGLE`, `ORDER_PANEL`, plus OE
pref-list blob tables for component defaults. Epic SmartSets carry cascading logic,
conditional sub-items, defaults, and dynamic-value rules that don't map cleanly to
PlanDefinition without an explicit transformation.

---

## Extraction Paths (ranked by practicality)

### 1. Clarity — most accessible
Nightly-refreshed SQL (SQL Server or Oracle). Contains the full order set / SmartSet /
SmartGroup structure including component orders, defaults, panels, and the cascading
group hierarchy. Most "dump order sets to files" projects start here. Write a job that
flattens Clarity tables into your app's JSON shape — optionally a FHIR-ish
`PlanDefinition` envelope so downstream stays standards-flavored.

### 2. Caboodle
Epic's Cogito enterprise data warehouse. Same data, dimensional model, often easier to
query than raw Clarity. Increasingly the preferred read store at Epic sites. Caveat
raised later: narrower model than Clarity, generally insufficient for order set
*configuration* data.

### 3. EHI Export
ONC-required Electronic Health Information export. Has a documented `ORDER_SMARTSET`
table family at open.epic.com/EHITables. Intended for patient-level EHI portability,
but the schema is published and some sites use it as a structured dump path.

**Important caveat (see below):** EHI Export is patient-scoped and is *not* a natural
channel for the master order set library.

### 4. Chronicles / Epic Bridges (Interconnect web services)
Epic exposes SOAP/REST web services beyond FHIR through Interconnect. Vendor Services
can grant access to data exchange methods not on open.epic. Ask Epic directly whether
an order set extract web service exists for the customer's contract; some sites have
one wired via Bridges. Least public-spec'd, most "talk to your Epic TS."

### 5. Reporting Workbench / Clarity SSRS
Not an API, but if a periodic flat-file dump is all that's needed, a scheduled report
against Clarity → CSV/JSON in a watched folder is the lowest-friction option and is how
a lot of downtime tooling actually ships.

### Also inventoried
Foundation/BCA report PDFs. Epic BCA keeps users in read-only access during outages with
paper documentation as fallback. Not structured data, but worth inventorying what's
already wired up before proposing a parallel path.

---

## Critical Distinction: Order Sets Are Config, Not Patient Data

Order sets are institutional templates, not patient records. They live in Chronicles
master files as configuration content — authored by clinical committees, maintained by
Epic build analysts, shared across the entire patient population at that institution.

A clinician selecting an order set generates patient-specific *instances* of the
component orders (which become `ORDER_PROC` and `ORDER_MED` records tied to that
patient's encounter), but the order set template itself is one row in the master file
regardless of how many patients use it.

**Consequence:** even though `ORDER_SMARTSET` appears in the EHI table catalog, the
patient-scoped EHI Export only carries records of which order sets were *applied* to a
given patient — not the master library of available order sets. The master library
extract must come from Clarity (or directly from Chronicles via Bridges / Reporting
Workbench), as a config dump, on a governance-driven refresh cadence rather than
per-patient.

**Why this matters for scoping the IT ask:** you're requesting a one-shot-plus-refresh
extract of master file tables, not a patient data pull. That is a different — and much
lighter — security/IRB conversation.

---

## Write-Back Is the Easy Half

Getting downtime-captured orders back into Epic has better FHIR-shaped options than the
export side. Epic supports write/create for `ServiceRequest`, `MedicationRequest`,
`NutritionOrder`, and several others in R4. HL7v2 `ORM^O01` also works.

Caveat: write operations require specific permissions and site-level approval beyond
read access. Start that conversation early.

---

## Loader Design Guidance

1. **Join on documented keys.** Epic publishes the standard schema; build against it.
2. **Expect site customizations.** Customers add custom columns and sometimes custom
   tables (`*_CUST` tables, extension columns). The TSV reader should tolerate unknown
   columns gracefully — warn, don't fail — and ignore unrecognized tables.
3. **Capture version metadata.** Record Epic version, extract timestamp, and customer
   site ID in the dump so the app can detect schema drift. Epic updates the EHI table
   schema with each release (roughly 4/year); columns get added and renamed.
4. **Target design:** "Drop these N table files in this S3/SFTP/blob location. We do the
   rest." Defensible to customer IT because everything they produce is already
   documented in Epic's own published schema.

---

## Prior Art

Josh Mandel (now at Microsoft Health & Life Sciences; key author on SMART on FHIR) has
open-source tooling for exactly this format.

- **EHI Living Manual** — https://joshuamandel.com/ehi-living-manual/
  Long-form companion documentation on Epic's data architecture: Chronicles vs Clarity,
  MUMPS legacy, table conventions like the `_2`/`_3` continuation pattern, the `LINE`
  column for multi-value attributes. Start with
  https://joshuamandel.com/ehi-living-manual/00-04-epic-data-architecture/
- **my-health-data-ehi-wip** — https://github.com/jmandel/my-health-data-ehi-wip
  Exploratory repo with scripts for processing EHI Export TSV data: TSV redaction,
  schema-driven TSV→JSON conversion, related-table merging via foreign-key inference,
  TypeScript codegen from the schema, and SQLite materialization. Even without using the
  code directly, the pipeline shape (TSV in → typed JSON out → relational query layer) is
  the loader architecture you'd want.

Different data scope from a Clarity config dump, but the same TSV-with-Epic-schema
plumbing. Worth reaching out directly with table-model questions.

---

## Recommended Architecture

1. Extract order sets from **Clarity** (or Chronicles via Bridges) as a scheduled config
   dump.
2. Transform **server-side** into `PlanDefinition` + `ActivityDefinition` JSON.
3. Offline app consumes **FHIR-native artifacts** — the runtime stays FHIR-shaped; only
   upstream extraction is non-FHIR.
4. Write back captured orders via FHIR `ServiceRequest.Create` /
   `MedicationRequest.Create` or HL7v2 `ORM^O01`.

CPG-on-FHIR IG is a reasonable reference for the target shape, but expect custom handling
for Epic's conditional/cascading SmartSet logic, which CPG-on-FHIR does not model 1:1.

---

## Extension: LLM-Assisted Ordering

The second chat drifted into using the order set extract as a **grounding corpus** for
AI-assisted order entry.

**Core idea:** fixed templates with parameterizable slots, nothing freelanced — the same
constraint Epic itself applies to SmartSets. The LLM performs the
human-language-to-template-binding step currently done by hunting through a dropdown.

**Why grounding matters:** without the extract, an LLM proposing orders has no idea what
that institution's sepsis bundle contains, what their vancomycin trough protocol is, or
whether they default to cefepime vs. pip-tazo for empiric coverage. With it, every
proposal is grounded in the institutional standard as approved by P&T and order set
governance. That is also the safety story for a CMIO conversation: the LLM cannot
recommend an order the institution hasn't already approved.

**Structured output is non-negotiable.** Use tool-use / structured output against a JSON
schema, e.g.:

```json
{
  "selected_plan_definitions": [
    {
      "id": "ED_SEPSIS_ADULT_v7",
      "included_actions": ["lactate", "blood_cx_x2", "vanc_load", "ns_30mlkg"],
      "excluded_default_actions": ["foley_placement"],
      "parameter_overrides": {
        "vanc_load": { "dose_mg_per_kg": 25, "indication": "MRSA_risk" }
      },
      "rationale_for_provider": "..."
    }
  ]
}
```

Deterministic code then turns that into FHIR resources. The LLM never emits FHIR
directly — too many failure modes, and "looks like valid FHIR" ≠ "clinically valid
against Epic's catalog."

**Conversational refinement is the actual UX win.** The differentiator isn't "type a
prompt, get orders" — it's "type a prompt, see proposed orders, say *use vanc instead of
cefepime, hold antibiotics pending blood culture*, watch the proposal update." Each turn
re-runs with conversation history; final submission is a single explicit provider action.
The transcript becomes part of the audit trail.

**Audit and Provenance are mandatory.** For every submitted order, create a `Provenance`
resource linking the prompt, model + version, proposed plan, provider edits, and
timestamp. This is the regulatory posture and the post-hoc eval data source in one. Epic
supports `Provenance.Create`, and several customers already use it for AI-assisted
documentation provenance.

**Eval:** this is `llm-fhir-query-eval` rotated 90 degrees — instead of NL → FHIR query,
it's NL + patient context + catalog → order proposal. Build against Synthea patients plus
a synthetic (or de-identified real) order set catalog. Score on:

- **Catalog grounding** — did the proposal reference only catalog items? Hard pass/fail,
  must be 100%.
- **Recall** of expected components for the clinical scenario.
- **Precision** — no spurious components.

---

## References

### FHIR / Standards
- FHIR R4 `PlanDefinition` — base spec establishes order sets as the canonical use case
- CPG-on-FHIR Implementation Guide
- HL7 CDS Knowledge Artifact Specification —
  https://www.hl7.org/implement/standards/product_brief.cfm?product_id=337
  (historical lineage; source of the "Low Suicide Risk" canonical example)
- SMART App Launch Framework — https://hl7.org/fhir/smart-app-launch/
- CDS Hooks — https://cds-hooks.org/

### Epic-Specific
- Epic on FHIR Specifications — https://fhir.epic.com/Specifications
- Epic EHI Tables Export Specification — https://open.epic.com/EHITables
  (column-level schema for Clarity tables, including `ORDER_SMARTSET`)
- open.epic Interfaces — https://open.epic.com/interface/FHIR

### Tooling and Prior Art
- EHI Living Manual — https://joshuamandel.com/ehi-living-manual/
- my-health-data-ehi-wip — https://github.com/jmandel/my-health-data-ehi-wip

---

## Glossary

- **Order Set** — institution-defined template of related orders (SmartSet in Epic),
  applied to a patient as a unit. Owned by clinical governance; not patient-specific.
- **SmartSet / SmartGroup** — Epic-internal names for order sets and their nested groups.
- **Chronicles** — Epic's underlying MUMPS/IRIS database; source of truth.
- **Clarity** — Epic's relational reporting database (SQL Server or Oracle); nightly ETL
  from Chronicles. Source of the order set extract.
- **Caboodle** — Epic's enterprise data warehouse; narrower model than Clarity, generally
  insufficient for order set configuration data.
- **EHI Export** — ONC-mandated patient-level health information export; documented TSV
  format with standardized column schemas.
- **BCA** — Epic Business Continuity Access; read-only access during outages.
- **Interconnect / Bridges** — Epic's non-FHIR web services and interface engine.
- **CDS Hooks** — spec for the EHR to call external services at workflow points
  (`patient-view`, `order-select`).
- **SMART on FHIR** — spec for launching external apps from an EHR with patient/user
  context and OAuth2 scopes.
- **PlanDefinition** — FHIR resource for the definition of a plan (template, protocol,
  order set).
- **ActivityDefinition** — FHIR resource for an individual activity definition (a single
  order template) referenced by a PlanDefinition.
- **RequestGroup** — FHIR resource grouping related requests instantiated from a
  PlanDefinition.
- **Provenance** — FHIR resource recording the origin of another resource; used here for
  AI-recommendation audit.

---

## Source Chats

- https://claude.ai/chat/78fab67f-a414-4443-a0ec-bd3211606a02 — original: FHIR
  availability check, extraction path survey
- https://claude.ai/chat/61b437a9-46ef-45ae-b055-8273dfa0e712 — follow-up: loader design,
  config-vs-patient-data distinction, prior art, LLM-assisted ordering extension
