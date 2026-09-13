"""Best-effort direct FHIR reads that fill whatever prefetch did not supply.

Everything here is fail-soft: a timeout, a 404, an auth error or a malformed
payload leaves the summary exactly as it was and appends a short note.  No PHI
is ever logged -- only resource types and status codes.
"""

import asyncio
import logging

import httpx

from epicvibe.proposal.patient_summary import (PatientSummary, _concept_codings,
                                               _medication_concept, bundle_resources,
                                               parse_allergy, parse_encounter,
                                               parse_observation, parse_patient,
                                               parse_service_request)

log = logging.getLogger("epicvibe.proposal")

DEFAULT_TIMEOUT = 1.5


def _auth_headers(fhir_authorization: dict | None) -> dict[str, str]:
    headers = {"Accept": "application/fhir+json"}
    token = (fhir_authorization or {}).get("access_token")
    if token:
        token_type = (fhir_authorization or {}).get("token_type") or "Bearer"
        headers["Authorization"] = f"{token_type} {token}"
    return headers


def search_paths(patient_id: str, encounter_id: str | None) -> dict[str, str]:
    paths = {
        "patient": f"Patient/{patient_id}",
        "observations": f"Observation?patient={patient_id}&_count=50",
        "allergies": f"AllergyIntolerance?patient={patient_id}",
        "conditions": f"Condition?patient={patient_id}",
        "medications": f"MedicationRequest?patient={patient_id}",
        "serviceRequests": f"ServiceRequest?patient={patient_id}",
    }
    if encounter_id:
        paths["encounter"] = f"Encounter/{encounter_id}"
    return paths


async def fetch_json(client: httpx.AsyncClient, base: str, path: str,
                     headers: dict[str, str], timeout: float) -> dict | None:
    url = f"{base.rstrip('/')}/{path}"
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        log.info("fhir read failed (%s): %s", path.split("?")[0], type(exc).__name__)
        return None
    if resp.status_code >= 400:
        log.info("fhir read non-2xx (%s): %s", path.split("?")[0], resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:
        log.info("fhir read unparseable (%s)", path.split("?")[0])
        return None
    return body if isinstance(body, dict) else None


def _missing_keys(summary: PatientSummary) -> list[str]:
    missing = []
    if summary.demographics.age is None and not summary.demographics.sex:
        missing.append("patient")
    if not summary.observations:
        missing.append("observations")
    if not summary.allergies:
        missing.append("allergies")
    if not summary.conditions:
        missing.append("conditions")
    if not summary.medications:
        missing.append("medications")
    if summary.encounter is None and summary.encounter_id:
        missing.append("encounter")
    if not summary.active_orders:
        missing.append("serviceRequests")
    return missing


def merge_payload(summary: PatientSummary, key: str, payload: dict) -> int:
    """Merge one fetched payload into the summary.  Returns items added."""
    resources = bundle_resources(payload)
    if not resources:
        return 0
    added = 0
    if key == "patient":
        summary.demographics = parse_patient(resources[0])
        added = 1
    elif key == "observations":
        for res in resources:
            parsed = parse_observation(res)
            if parsed:
                summary.observations.append(parsed)
                added += 1
    elif key == "allergies":
        for res in resources:
            parsed = parse_allergy(res)
            if parsed:
                summary.allergies.append(parsed)
                added += 1
    elif key == "conditions":
        for res in resources:
            for coded in _concept_codings(res.get("code")):
                summary.conditions.append(coded)
                added += 1
    elif key == "medications":
        for res in resources:
            for coded in _concept_codings(_medication_concept(res)):
                summary.medications.append(coded)
                added += 1
    elif key == "encounter":
        parsed = parse_encounter(resources[0])
        if parsed:
            summary.encounter = parsed
            added = 1
    elif key == "serviceRequests":
        for res in resources:
            parsed = parse_service_request(res)
            if parsed:
                summary.active_orders.append(parsed)
                added += 1
    return added


async def fetch_missing(summary: PatientSummary, fhir_server: str | None,
                        fhir_authorization: dict | None, patient_id: str,
                        encounter_id: str | None = None,
                        client: httpx.AsyncClient | None = None,
                        timeout: float = DEFAULT_TIMEOUT) -> PatientSummary:
    """Fill gaps in `summary` from the EHR's FHIR endpoint.  Never raises."""
    if not fhir_server:
        return summary
    missing = _missing_keys(summary)
    if not missing:
        return summary
    paths = search_paths(patient_id, encounter_id)
    wanted = [(k, paths[k]) for k in missing if k in paths]
    if not wanted:
        return summary
    headers = _auth_headers(fhir_authorization)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[fetch_json(client, fhir_server, p, headers, timeout)
                             for _, p in wanted], return_exceptions=True),
            timeout=timeout * len(wanted) + timeout)
    except Exception:
        log.info("fhir pull aborted after timeout budget")
        summary.notes.append("live FHIR pull timed out")
        results = []
    finally:
        if owns_client:
            try:
                await client.aclose()
            except Exception:
                pass
    filled = []
    for (key, _), payload in zip(wanted, results):
        if isinstance(payload, dict):
            if merge_payload(summary, key, payload):
                filled.append(key)
    if filled:
        summary.notes.append("live FHIR pull filled: " + ",".join(sorted(filled)))
    return summary
