# Spike: FHIR Order Write-Back Against Epic Sandbox

**Status:** In progress (2026-08-05)
**Question:** Can orders be written into Epic over plain FHIR (outside a CDS Hooks
interaction)? Determines the downtime write-back channel priority (spec
`2026-08-04-downtime-ordering-design.md` §4).

## Findings So Far

### 1. Sandbox CapabilityStatement (unauthenticated, definitive for what's routable)

`GET https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/metadata` (FHIR 4.0.1, software "Epic"):

| Resource | Declared interactions |
|---|---|
| **ServiceRequest** | **create**, read, search-type, **update** |
| MedicationRequest | read, search-type (NO create) |
| Condition | create, read, search-type |
| DocumentReference | create, read, search-type, update |

Notable: `ServiceRequest.create` IS declared in the conformance statement. Public
documentation lists Epic's only order-create as "CDS Hooks ServiceRequest.Create
(Unsigned Order)" — the declared create is presumed to be that API surfacing in
conformance, but conformance alone doesn't reveal the authorization context
restriction. **Empirical POST with a standard (non-CDS-Hooks) token is the test.**

`MedicationRequest` has no create in any context on the public sandbox — medication
order write-back over plain FHIR is ruled out at this sandbox's surface, full stop.

### 2. Unauthenticated POST probe

`POST /ServiceRequest` (draft HbA1c order, sandbox test patient Camila Lopez) →
**401, `WWW-Authenticate: Bearer`**, empty body. The route exists and is gated on
auth only (a nonexistent operation typically yields 404/405 or an OperationOutcome).

## Remaining Steps

- [ ] Obtain an access token for the registered sandbox app (needs Client ID —
      requested from app owner; flow depends on app type: backend client_credentials
      vs SMART auth-code).
- [ ] Authenticated `POST /ServiceRequest` (status draft → and if rejected, retry
      with status active, intent order/proposal variants) — record status code +
      OperationOutcome verbatim.
- [ ] If create succeeds: `GET` the created resource back; check what an unsigned
      order looks like; test `update`.
- [ ] Inspect fhir.epic.com API picker (manual, requires portal login) for the exact
      names of ServiceRequest write APIs available to app registrations.
- [ ] Customer TS question: does their contract expose any site-specific FHIR order
      write beyond the sandbox surface?

## Interim Read on the Downtime Write-Back Question

Even in the best case, plain-FHIR write-back covers **procedure/lab/imaging orders
only** (ServiceRequest); medication orders have no FHIR create path and would need
HL7v2 ORM (or per-site mechanisms) regardless. Channel priority in the downtime spec
should anticipate a split: FHIR for ServiceRequest if the authenticated attempt
succeeds, HL7v2 for meds (or for everything, for uniformity).
