# Spike: FHIR Order Write-Back Against Epic Sandbox

**Status:** COMPLETE (2026-08-06) — **FHIR order write-back is not viable; HL7v2 becomes the primary downtime channel.**
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

## Authenticated Attempt (2026-08-06) — DEFINITIVE

App: fhir.epic.com registration, audience "Clinicians or Administrative Users",
confidential client (non-prod client ID 7d3f043c-e73e-472d-a196-91e6226179ee),
order-related APIs selected including both CDS Hooks (Unsigned Order) creates and
`ServiceRequest.Create (External Radiotherapy Summary)`.

Flow: SMART standalone authorization-code (Playwright-captured redirect; sandbox
provider login FHIR/EpicFhir11!) → token exchange with client secret → 200.

**Scopes Epic granted to the user-context token (read-only, despite create APIs
being selected on the app):**
`user/MedicationDispense.read user/MedicationRequest.read user/NutritionOrder.read
user/Patient.read user/Procedure.read user/ServiceRequest.read offline_access`

| Attempt | Result | Meaning |
|---|---|---|
| `GET Patient/{id}` | 200 | Token valid, reads work |
| `POST ServiceRequest` (status draft) | **403**, empty body | Operation exists but is not authorized for a standard user token — the create APIs are context-bound (CDS Hooks / niche radiotherapy), not grantable to a general SMART session |
| `POST ServiceRequest` (status active) | **403** | Same |
| `POST MedicationRequest` | **405** "resource does not support http method 'POST'" | No medication order create exists at all, in any context |

### Conclusion

1. **General-purpose FHIR order write-back into Epic is not possible** on the public
   sandbox surface — empirically confirmed, not just documentation-inferred.
   ServiceRequest creates exist only inside a CDS Hooks interaction (unsigned orders)
   or the niche External Radiotherapy Summary API; MedicationRequest has no create,
   full stop.
2. **Downtime write-back channel priority is now settled:** HL7v2 ORM via Bridges is
   primary (all order types), recovery worklist is the day-one fallback,
   `DocumentReference.Create` for the chart documentation note (add that API to the
   app registration when needed).
3. Residual (low-probability) question for the customer's Epic TS: any site-specific
   order-write mechanism in their contract beyond the public surface.

## Addendum (2026-08-06): Why the Create APIs Exist — Mystery Solved

Epic's own machine-readable spec records (fhir.epic.com/Specifications/Api?id=1060/1062)
show the CDS Hooks Create (Unsigned Order) APIs have **no HTTP method and no URL
template — they are not REST endpoints at all.** Their documented "sample response" is
a CDS Hooks card JSON. Selecting them on an app registration (a) authorizes that CDS
service's suggestions to create unsigned orders when accepted, and (b) gates whether
Epic sends `draftOrders` context to the service. "Invoking" the API IS returning the
resource inside a suggestion action; Epic files the order internally from its own
master-file defaults. The hook's `fhirAuthorization` token even carries create-named
scopes, but there is nothing to POST to — only the companion Read (Unsigned Order)
endpoints are real REST (for reading draft orders during the hook window). The
Feb-2024 `ServiceRequest.Update (Unsigned Order)` systemAction is likewise
response-embedded and annotation-only (Da Vinci CRD pattern).

**Backend Systems audience would not help:** the only public primary evidence (Josh
Mandel's chat.fhir.org walkthrough, with Epic staff participating) shows backend
sandbox tokens receive read-only scopes; Epic steers backend write use cases to HL7v2.
`ServiceRequest.Create (External Radiotherapy Summary)` is a CodeX radiotherapy
documentation intake for oncology systems (Varian/Elekta-class), not CPOE ordering.

**Consumption path for the write capability = exactly our Phase 1 CDS service.** The
remaining ways to see acceptance behavior without a customer environment: Epic's
simulator supports patient-view only; Vendor Services membership (~$1,700/yr) includes
the expanded test sandbox/harness where suggestion acceptance can be exercised.

Key sources: fhir.epic.com/Specifications?api=1060, ?api=1062, ?api=10643,
Documentation?docId=cds-hooks; chat-archive.fhir.org (Epic backend services; CDS Hook
Simulator threads); open.epic.com/Interface/FHIR; healthapiguy.substack.com.

## Original Step Checklist (superseded by the above)

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
