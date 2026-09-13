"""Thin FHIR read/write helpers for the SMART app.

All reads are fail-soft (a failure yields an empty list and a note); writes
surface their error to the caller so the UI can show what did not go through.
No PHI is logged.
"""

import asyncio
import logging

import httpx

from epicvibe.proposal.patient_summary import bundle_resources

log = logging.getLogger("epicvibe.smart")

READ_TIMEOUT = 10.0
WRITE_TIMEOUT = 10.0

PATIENT_QUERIES: list[tuple[str, str]] = [
    ("Condition", "Condition?patient={pid}"),
    ("MedicationRequest", "MedicationRequest?patient={pid}"),
    ("Observation", "Observation?patient={pid}&_count=50"),
    ("AllergyIntolerance", "AllergyIntolerance?patient={pid}"),
    ("ServiceRequest", "ServiceRequest?patient={pid}"),
    ("Procedure", "Procedure?patient={pid}"),
]


def _url(iss: str, path: str) -> str:
    return f"{iss.rstrip('/')}/{path.lstrip('/')}"


async def read_json(client: httpx.AsyncClient, iss: str, path: str,
                    headers: dict[str, str], timeout: float = READ_TIMEOUT) -> dict | None:
    try:
        resp = await client.get(_url(iss, path),
                                headers={"Accept": "application/fhir+json", **headers},
                                timeout=timeout)
    except Exception as exc:
        log.info("smart fhir read failed (%s): %s", path.split("?")[0], type(exc).__name__)
        return None
    if resp.status_code >= 400:
        log.info("smart fhir read non-2xx (%s): %s", path.split("?")[0], resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def fetch_patient_data(client: httpx.AsyncClient, iss: str, headers: dict[str, str],
                             patient_id: str,
                             encounter_id: str = "") -> dict[str, list[dict]]:
    """Pull everything the app needs for one patient, in parallel."""
    paths: list[tuple[str, str]] = [("Patient", f"Patient/{patient_id}")]
    paths += [(kind, tmpl.format(pid=patient_id)) for kind, tmpl in PATIENT_QUERIES]
    if encounter_id:
        paths.append(("Encounter", f"Encounter/{encounter_id}"))

    results = await asyncio.gather(
        *[read_json(client, iss, path, headers) for _, path in paths],
        return_exceptions=True)

    out: dict[str, list[dict]] = {}
    for (kind, _), payload in zip(paths, results):
        if not isinstance(payload, dict):
            continue
        out.setdefault(kind, []).extend(bundle_resources(payload))
    return out


async def create_resource(client: httpx.AsyncClient, iss: str, headers: dict[str, str],
                          resource: dict) -> tuple[bool, str]:
    """POST one resource.  Returns (ok, id-or-error)."""
    resource_type = resource.get("resourceType", "")
    try:
        resp = await client.post(
            _url(iss, resource_type), json=resource, timeout=WRITE_TIMEOUT,
            headers={"Content-Type": "application/fhir+json",
                     "Accept": "application/fhir+json", **headers})
    except Exception as exc:
        log.info("smart fhir create failed (%s): %s", resource_type, type(exc).__name__)
        return False, f"{resource_type}: request failed ({type(exc).__name__})"
    if resp.status_code >= 400:
        log.info("smart fhir create non-2xx (%s): %s", resource_type, resp.status_code)
        return False, f"{resource_type}: server returned {resp.status_code}"
    created_id = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            created_id = body.get("id") or ""
    except Exception:
        pass
    if not created_id:
        location = resp.headers.get("Location") or resp.headers.get("Content-Location") or ""
        parts = [p for p in location.split("/") if p]
        if "_history" in parts:
            parts = parts[:parts.index("_history")]
        created_id = parts[-1] if parts else ""
    return True, created_id
