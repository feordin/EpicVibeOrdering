"""JSON API consumed by the mock EHR chart UI."""
from __future__ import annotations

import copy
import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("epicvibe.mockehr.api")

router = APIRouter(prefix="/api")

ORDER_TYPES = ("ServiceRequest", "MedicationRequest")
DRAFT_STATUSES = {"draft"}


# --------------------------------------------------------------------- helpers
def _human_name(resource: dict) -> str:
    names = resource.get("name") or []
    if not names:
        return resource.get("id", "Unknown")
    name = names[0]
    given = " ".join(name.get("given") or [])
    return " ".join(p for p in (given, name.get("family")) if p) or name.get("text", "Unknown")


def _age(birth_date: str | None) -> int | None:
    if not birth_date:
        return None
    try:
        born = date.fromisoformat(birth_date)
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _mrn(patient: dict) -> str:
    for ident in patient.get("identifier") or []:
        codings = ((ident.get("type") or {}).get("coding") or [])
        if any(c.get("code") == "MR" for c in codings) or not codings:
            return ident.get("value", "")
    return ""


def _concept_text(concept: dict | None) -> str:
    if not concept:
        return ""
    if concept.get("text"):
        return concept["text"]
    for coding in concept.get("coding") or []:
        if coding.get("display"):
            return coding["display"]
        if coding.get("code"):
            return coding["code"]
    return ""


def _codings(concept: dict | None) -> list[dict]:
    return [{"system": c.get("system", ""), "code": c.get("code", ""),
             "display": c.get("display", "")} for c in (concept or {}).get("coding") or []]


def _order_concept(resource: dict) -> dict | None:
    return resource.get("code") or resource.get("medicationCodeableConcept")


def _interpretation(resource: dict) -> str:
    for interp in resource.get("interpretation") or []:
        for coding in interp.get("coding") or []:
            return coding.get("code", "")
    return ""


def _obs_value(resource: dict) -> str:
    qty = resource.get("valueQuantity")
    if qty:
        return f"{qty.get('value')} {qty.get('unit', '')}".strip()
    if resource.get("valueString"):
        return resource["valueString"]
    if resource.get("valueCodeableConcept"):
        return _concept_text(resource["valueCodeableConcept"])
    parts = []
    for comp in resource.get("component") or []:
        cq = comp.get("valueQuantity") or {}
        parts.append(str(cq.get("value", "")))
    if parts:
        unit = ((resource.get("component") or [{}])[0].get("valueQuantity") or {}).get("unit", "")
        return f"{'/'.join(parts)} {unit}".strip()
    return ""


def _obs_category(resource: dict) -> str:
    for cat in resource.get("category") or []:
        for coding in cat.get("coding") or []:
            return coding.get("code", "")
    return ""


def _sort_key(resource: dict) -> str:
    return str(resource.get("effectiveDateTime") or resource.get("authoredOn")
               or resource.get("recordedDate") or "")


def _current_encounter(store, patient_id: str) -> dict | None:
    encounters = store.search("Encounter", {"patient": patient_id})
    in_progress = [e for e in encounters if e.get("status") == "in-progress"]
    pool = in_progress or encounters
    return pool[0] if pool else None


def _patient_row(store, patient: dict) -> dict:
    encounter = _current_encounter(store, patient["id"])
    problems = store.search("Condition", {"patient": patient["id"],
                                          "clinical-status": "active"})
    return {
        "id": patient["id"],
        "name": _human_name(patient),
        "gender": patient.get("gender", ""),
        "birthDate": patient.get("birthDate", ""),
        "age": _age(patient.get("birthDate")),
        "mrn": _mrn(patient),
        "encounterId": encounter["id"] if encounter else None,
        "encounterType": _concept_text((encounter.get("type") or [{}])[0]) if encounter else "",
        "encounterReason": (encounter.get("reasonCode") or [{}])[0].get("text", "")
                           if encounter else "",
        "problemCount": len(problems),
        "topProblem": _concept_text(problems[0].get("code")) if problems else "",
    }


def _order_row(resource: dict) -> dict:
    concept = _order_concept(resource)
    return {
        "id": resource["id"],
        "resourceType": resource["resourceType"],
        "reference": f"{resource['resourceType']}/{resource['id']}",
        "display": _concept_text(concept),
        "codings": _codings(concept),
        "status": resource.get("status", ""),
        "intent": resource.get("intent", ""),
        "authoredOn": resource.get("authoredOn", ""),
        "instructions": (resource.get("dosageInstruction") or [{}])[0].get("text", ""),
        "source": (resource.get("meta") or {}).get("source", ""),
    }


def collect_orders(store, patient_id: str) -> dict[str, list[dict]]:
    drafts, active = [], []
    for rtype in ORDER_TYPES:
        for resource in store.search(rtype, {"patient": patient_id}):
            row = _order_row(resource)
            if resource.get("status") in DRAFT_STATUSES:
                drafts.append(row)
            else:
                active.append(row)
    drafts.sort(key=lambda r: r["id"])
    active.sort(key=lambda r: r["id"])
    return {"drafts": drafts, "active": active}


def draft_orders_bundle(store, patient_id: str, fhir_base: str) -> dict:
    resources = []
    for rtype in ORDER_TYPES:
        resources.extend(r for r in store.search(rtype, {"patient": patient_id})
                         if r.get("status") in DRAFT_STATUSES)
    resources.sort(key=lambda r: r["id"])
    entries = [{"fullUrl": f"{fhir_base}/{r['resourceType']}/{r['id']}", "resource": r}
               for r in resources]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def build_chart(store, patient_id: str) -> dict | None:
    patient = store.get("Patient", patient_id)
    if patient is None:
        return None
    encounter = _current_encounter(store, patient_id)
    conditions = store.search("Condition", {"patient": patient_id, "clinical-status": "active"})
    medications = store.search("MedicationRequest", {"patient": patient_id, "status": "active"})
    allergies = store.search("AllergyIntolerance", {"patient": patient_id})
    observations = sorted(store.search("Observation", {"patient": patient_id}),
                          key=_sort_key, reverse=True)
    labs = [o for o in observations if _obs_category(o) != "vital-signs"]
    vitals = [o for o in observations if _obs_category(o) == "vital-signs"]

    def obs_row(o: dict) -> dict:
        return {"id": o["id"], "name": _concept_text(o.get("code")), "value": _obs_value(o),
                "when": _sort_key(o), "flag": _interpretation(o),
                "codings": _codings(o.get("code"))}

    return {
        "patient": _patient_row(store, patient),
        "encounter": {
            "id": encounter["id"], "status": encounter.get("status", ""),
            "class": (encounter.get("class") or {}).get("display", ""),
            "type": _concept_text((encounter.get("type") or [{}])[0]),
            "reason": (encounter.get("reasonCode") or [{}])[0].get("text", ""),
            "start": (encounter.get("period") or {}).get("start", ""),
        } if encounter else None,
        "problems": [{"id": c["id"], "display": _concept_text(c.get("code")),
                      "codings": _codings(c.get("code")),
                      "onset": c.get("onsetDateTime", ""),
                      "category": _concept_text((c.get("category") or [{}])[0])}
                     for c in conditions],
        "medications": [{"id": m["id"], "display": _concept_text(_order_concept(m)),
                         "codings": _codings(_order_concept(m)),
                         "sig": (m.get("dosageInstruction") or [{}])[0].get("text", ""),
                         "authoredOn": m.get("authoredOn", "")}
                        for m in medications],
        "allergies": [{"id": a["id"], "display": _concept_text(a.get("code")),
                       "criticality": a.get("criticality", ""),
                       "reaction": _concept_text(
                           ((a.get("reaction") or [{}])[0].get("manifestation") or [{}])[0])}
                      for a in allergies],
        "labs": [obs_row(o) for o in labs],
        "vitals": [obs_row(o) for o in vitals],
        "orders": collect_orders(store, patient_id),
    }


# ---------------------------------------------------------------- request models
class PatientRef(BaseModel):
    patientId: str


class DraftRequest(BaseModel):
    patientId: str
    text: str
    orderType: str = "ServiceRequest"


class AcceptRequest(BaseModel):
    patientId: str
    resource: dict
    serviceId: str | None = None
    cardUuid: str | None = None
    suggestionUuid: str | None = None


class RemoveDraftRequest(BaseModel):
    patientId: str
    reference: str


class LaunchRequest(BaseModel):
    patientId: str
    url: str


# ------------------------------------------------------------------- endpoints
@router.get("/patients")
async def list_patients(request: Request) -> dict:
    store = request.app.state.store
    patients = sorted(store.all_of("Patient"), key=lambda p: _human_name(p))
    return {"patients": [_patient_row(store, p) for p in patients]}


@router.get("/patients/{patient_id}/chart")
async def get_chart(patient_id: str, request: Request):
    chart = build_chart(request.app.state.store, patient_id)
    if chart is None:
        return JSONResponse({"error": f"unknown patient {patient_id}"}, status_code=404)
    return chart


@router.get("/cds/status")
async def cds_status(request: Request) -> dict:
    return request.app.state.hooks.discovery_status()


@router.post("/cds/refresh")
async def cds_refresh(request: Request) -> dict:
    return await request.app.state.hooks.refresh()


@router.get("/dev/history")
async def dev_history(request: Request) -> dict:
    hooks = request.app.state.hooks
    return {"discovery": hooks.discovery_status(), "history": hooks.history[::-1]}


@router.post("/hooks/patient-view")
async def hook_patient_view(body: PatientRef, request: Request) -> dict:
    return await _fire(request, "patient-view", body.patientId)


@router.post("/hooks/order-select")
async def hook_order_select(body: PatientRef, request: Request) -> dict:
    # `selections` must be non-empty whenever there are draft orders (CDS Hooks spec):
    # with no explicit selection, everything currently in the tray is selected.
    return await _fire_order_select(request, body.patientId, selections=None)


@router.post("/orders/draft")
async def add_draft(body: DraftRequest, request: Request) -> dict:
    store = request.app.state.store
    patient = store.get("Patient", body.patientId)
    if patient is None:
        return {"error": f"unknown patient {body.patientId}"}
    encounter = _current_encounter(store, body.patientId)
    rtype = body.orderType if body.orderType in ORDER_TYPES else "ServiceRequest"
    text = body.text.strip() or "Unspecified order"
    concept = {"text": text, "coding": [{"system": "http://epicvibe.example/mockehr-orderable",
                                         "code": text.lower().replace(" ", "-")[:64],
                                         "display": text}]}
    resource: dict[str, Any] = {
        "resourceType": rtype, "status": "draft", "intent": "proposal",
        "subject": {"reference": f"Patient/{body.patientId}"},
        "authoredOn": datetime.now().isoformat(timespec="seconds"),
        "requester": {"reference": request.app.state.settings.user_id},
        "meta": {"source": "manual"},
    }
    if rtype == "MedicationRequest":
        resource["medicationCodeableConcept"] = concept
    else:
        resource["code"] = concept
    if encounter:
        resource["encounter"] = {"reference": f"Encounter/{encounter['id']}"}
    created = store.create(rtype, resource)
    selection = f"{created['resourceType']}/{created['id']}"
    hook = await _fire_order_select(request, body.patientId, selections=[selection])
    return {"draft": _order_row(created), "hook": hook,
            "orders": collect_orders(store, body.patientId)}


@router.post("/orders/remove")
async def remove_draft(body: RemoveDraftRequest, request: Request) -> dict:
    store = request.app.state.store
    rtype, _, rid = body.reference.partition("/")
    resource = store.get(rtype, rid)
    if resource is not None and resource.get("status") in DRAFT_STATUSES:
        store._by_type[rtype].pop(rid, None)
    return {"orders": collect_orders(store, body.patientId)}


@router.post("/suggestions/accept")
async def accept_suggestion(body: AcceptRequest, request: Request) -> dict:
    """File a card suggestion's `create` action into the store as a draft order."""
    store = request.app.state.store
    resource = dict(body.resource or {})
    rtype = resource.get("resourceType")
    if rtype not in ORDER_TYPES:
        return {"error": f"unsupported suggestion resourceType: {rtype}"}
    resource.setdefault("subject", {"reference": f"Patient/{body.patientId}"})
    resource["status"] = "draft"
    resource.setdefault("intent", "proposal")
    encounter = _current_encounter(store, body.patientId)
    if encounter and "encounter" not in resource:
        resource["encounter"] = {"reference": f"Encounter/{encounter['id']}"}
    resource.setdefault("authoredOn", datetime.now().isoformat(timespec="seconds"))
    resource["meta"] = {"source": "cds-suggestion"}
    created = store.create(rtype, resource)

    feedback = {"sent": False, "reason": "no serviceId"}
    if body.serviceId:
        feedback = await request.app.state.hooks.send_feedback(
            body.serviceId, body.cardUuid, body.suggestionUuid, outcome="accepted")
    return {"created": _order_row(created), "feedback": feedback,
            "orders": collect_orders(store, body.patientId)}


@router.post("/orders/sign")
async def sign_orders(body: PatientRef, request: Request) -> dict:
    store = request.app.state.store
    settings = request.app.state.settings
    encounter = _current_encounter(store, body.patientId)
    bundle = draft_orders_bundle(store, body.patientId, settings.fhir_base)
    hook = await request.app.state.hooks.fire_hook(
        "order-sign", patient_id=body.patientId,
        encounter_id=encounter["id"] if encounter else None,
        extra_context={"draftOrders": bundle})

    critical = [c for c in (hook.get("cards") or [])
                if str(c.get("indicator", "")).lower() == "critical"]
    if critical:
        # A critical order-sign card is a hard stop: the drafts stay drafts.
        return {"hook": hook, "blocked": True, "signed": [],
                "blockedBy": [c.get("summary", "") for c in critical],
                "orders": collect_orders(store, body.patientId)}

    signed = []
    for entry in bundle.get("entry") or []:
        # The bundle's resources are the live store dicts and are also embedded in the
        # recorded hook request -- mutating them in place would rewrite history.
        resource = copy.deepcopy(entry["resource"])
        resource["status"] = "active"
        resource["intent"] = "order"
        store.put(resource)
        signed.append(f"{resource['resourceType']}/{resource['id']}")
    return {"hook": hook, "blocked": False, "signed": signed,
            "orders": collect_orders(store, body.patientId)}


@router.post("/smart/launch")
async def smart_launch(body: LaunchRequest, request: Request) -> dict:
    store = request.app.state.store
    settings = request.app.state.settings
    encounter = _current_encounter(store, body.patientId)
    ctx = request.app.state.oauth.create_launch(
        body.patientId, encounter["id"] if encounter else None, settings.user_id)
    sep = "&" if "?" in body.url else "?"
    url = f"{body.url}{sep}iss={settings.fhir_base}&launch={ctx.launch_id}"
    return {"launchId": ctx.launch_id, "url": url,
            "patientId": ctx.patient_id, "encounterId": ctx.encounter_id}


@router.post("/reset")
async def reset(request: Request) -> dict:
    app = request.app
    app.state.store.load()
    app.state.oauth.reset()
    app.state.hooks.history.clear()
    return {"reset": True, "resources": app.state.store.summary()}


# ----------------------------------------------------------------------- inner
async def _fire(request: Request, hook: str, patient_id: str,
                extra_context: dict | None = None) -> dict:
    store = request.app.state.store
    encounter = _current_encounter(store, patient_id)
    return await request.app.state.hooks.fire_hook(
        hook, patient_id=patient_id,
        encounter_id=encounter["id"] if encounter else None,
        extra_context=extra_context)


async def _fire_order_select(request: Request, patient_id: str,
                             selections: list[str] | None) -> dict:
    settings = request.app.state.settings
    bundle = draft_orders_bundle(request.app.state.store, patient_id, settings.fhir_base)
    if selections is None:
        selections = [f"{e['resource']['resourceType']}/{e['resource']['id']}"
                      for e in bundle.get("entry") or []]
    return await _fire(request, "order-select", patient_id,
                       extra_context={"selections": selections, "draftOrders": bundle})
