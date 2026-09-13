import logging

from fastapi import APIRouter, Depends, Request

from epicvibe.cache import refinement_cache
from epicvibe.cds.auth import epic_auth
from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                render_summary_card)
from epicvibe.inference.base import describe_provider
from epicvibe.proposal import fhir_pull
from epicvibe.proposal.patient_summary import summarize_prefetch

log = logging.getLogger("epicvibe.cds")
router = APIRouter(dependencies=[Depends(epic_auth)])
discovery_router = APIRouter()

PREFETCH = {
    "conditions": "Condition?patient={{context.patientId}}&clinical-status=active",
    "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
    "patient": "Patient/{{context.patientId}}",
    "observations": "Observation?patient={{context.patientId}}&_count=50",
    "allergies": "AllergyIntolerance?patient={{context.patientId}}",
    "encounter": "Encounter/{{context.encounterId}}",
}
SERVICES = [
    {"id": "epicvibe-patient-view", "hook": "patient-view",
     "title": "EpicVibe Order Review", "description": "AI order-set suggestions", "prefetch": PREFETCH},
    {"id": "epicvibe-order-select", "hook": "order-select",
     "title": "EpicVibe Order Suggestions", "description": "AI order-set suggestions", "prefetch": PREFETCH},
    {"id": "epicvibe-order-sign", "hook": "order-sign",
     "title": "EpicVibe Order Completeness", "description": "Protocol completeness check", "prefetch": PREFETCH},
]


def cache_key(context: dict) -> str:
    return context.get("encounterId") or f"pat:{context['patientId']}"


def refinement_for(state, context: dict):
    """The clinician-refined set handed back from the SMART app, if one is live.

    Looked up by encounter first, then patient: a SMART launch may carry no
    encounter context, and the hook always does.
    """
    try:
        return refinement_cache(state).get_for(context.get("encounterId"),
                                               context.get("patientId"))
    except Exception:
        log.info("refinement lookup failed")
        return None


def _contained_medication_concept(res: dict) -> dict:
    """Resolve medicationReference -> contained[] Medication (Epic Feb-2024+ style)."""
    ref = (res.get("medicationReference") or {}).get("reference", "")
    if not ref.startswith("#"):
        return {}
    contained_id = ref[1:]
    for contained in res.get("contained", []):
        if contained.get("resourceType") == "Medication" and contained.get("id") == contained_id:
            return contained.get("code") or {}
    return {}


def _draft_codes(context: dict) -> frozenset[str]:
    codes = set()
    for entry in (context.get("draftOrders") or {}).get("entry", []):
        res = entry.get("resource", {})
        concept = (res.get("medicationCodeableConcept") or res.get("code")
                  or _contained_medication_concept(res) or {})
        for coding in concept.get("coding", []):
            if coding.get("code"):
                codes.add(coding["code"])
    return frozenset(codes)


def _reason_concepts(context: dict, prefetch: dict) -> list[dict]:
    """CodeableConcepts for ServiceRequest.reasonCode, from the problem list."""
    summary = summarize_prefetch(context, prefetch)
    return [{"coding": [{"system": c.system, "code": c.code, "display": c.display}],
             "text": c.display or c.code}
            for c in summary.conditions if c.code][:1]


def _enqueue_generate(state, key: str, context: dict, body: dict) -> None:
    prefetch = body.get("prefetch", {}) or {}
    summary = summarize_prefetch(context, prefetch)
    settings = state.settings
    fhir_server = body.get("fhirServer")
    fhir_authorization = body.get("fhirAuthorization")

    async def job():
        if settings.fhir_pull_enabled and fhir_server:
            # Best-effort: use everything the EHR will give us, not just prefetch.
            try:
                await fhir_pull.fetch_missing(
                    summary, fhir_server, fhir_authorization,
                    summary.patient_id, summary.encounter_id,
                    client=getattr(state, "fhir_client", None),
                    timeout=settings.fhir_timeout_seconds)
            except Exception:
                log.info("live FHIR pull failed; continuing with prefetch only")
        vp = await state.engine.generate(summary)
        state.cache.put(key, vp)
        state.audit.record_proposal(key, vp, model=describe_provider(state.engine.provider))

    state.runner.enqueue(key, job)


@discovery_router.get("/cds-services")
async def discovery() -> dict:
    return {"services": SERVICES}


@router.post("/cds-services/epicvibe-patient-view")
async def patient_view(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        context = body["context"]
        key = cache_key(context)
        refined = refinement_for(state, context)
        if refined is not None and not refined.refined.is_empty:
            return {"cards": [render_summary_card(refined.refined, state.index,
                                                  state.settings.smart_launch_url,
                                                  reviewed=True)]}
        vp = state.cache.get(key)
        if vp is not None:
            if vp.is_empty:
                return {"cards": []}
            return {"cards": [render_summary_card(vp, state.index,
                                                  state.settings.smart_launch_url)]}
        _enqueue_generate(state, key, context, body)
    except Exception:
        log.exception("patient-view handler failed")
    return {"cards": []}


@router.post("/cds-services/epicvibe-order-select")
async def order_select(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        context = body["context"]
        key = cache_key(context)
        # A set the clinician reviewed in the SMART app wins over the raw AI
        # proposal: this is the write channel back into Epic.
        refined = refinement_for(state, context)
        vp = refined.refined if refined is not None else state.cache.get(key)
        if vp is None:
            _enqueue_generate(state, key, context, body)
            return {"cards": []}
        cards = render_suggestion_cards(
            vp, state.index, context["patientId"],
            exclude_codes=_draft_codes(context),
            encounter_id=context.get("encounterId"),
            user_id=context.get("userId"),
            reason_concepts=_reason_concepts(context, body.get("prefetch", {}) or {}),
            smart_launch_url=state.settings.smart_launch_url,
            reviewed=refined is not None)
        return {"cards": cards}
    except Exception:
        log.exception("order-select handler failed")
        return {"cards": []}


@router.post("/cds-services/epicvibe-order-sign")
async def order_sign(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        context = body["context"]
        refined = refinement_for(state, context)
        vp = refined.refined if refined is not None else state.cache.get(cache_key(context))
        if vp is None:
            return {"cards": []}
        card = render_missing_items_card(vp, state.index, _draft_codes(context),
                                         state.settings.smart_launch_url,
                                         reviewed=refined is not None)
        return {"cards": [card] if card else []}
    except Exception:
        log.exception("order-sign handler failed")
        return {"cards": []}


@router.post("/cds-services/{service_id}/feedback")
async def feedback(service_id: str, request: Request) -> dict:
    state = request.app.state
    try:
        state.audit.record_feedback(service_id, await request.json())
    except Exception:
        log.exception("feedback handler failed")
    return {}
