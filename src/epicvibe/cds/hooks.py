import logging

from fastapi import APIRouter, Request

from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                render_summary_card)
from epicvibe.proposal.patient_summary import summarize_prefetch

log = logging.getLogger("epicvibe.cds")
router = APIRouter()

PREFETCH = {
    "conditions": "Condition?patient={{context.patientId}}&clinical-status=active",
    "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
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


def _draft_codes(context: dict) -> frozenset[str]:
    codes = set()
    for entry in (context.get("draftOrders") or {}).get("entry", []):
        res = entry.get("resource", {})
        concept = res.get("medicationCodeableConcept") or res.get("code") or {}
        for coding in concept.get("coding", []):
            if coding.get("code"):
                codes.add(coding["code"])
    return frozenset(codes)


def _enqueue_generate(state, key: str, context: dict, prefetch: dict) -> None:
    summary = summarize_prefetch(context, prefetch)

    async def job():
        vp = await state.engine.generate(summary)
        state.cache.put(key, vp)
        state.audit.record_proposal(key, vp, model=state.settings.anthropic_model)

    state.runner.enqueue(key, job)


@router.get("/cds-services")
async def discovery() -> dict:
    return {"services": SERVICES}


@router.post("/cds-services/epicvibe-patient-view")
async def patient_view(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        key = cache_key(body["context"])
        vp = state.cache.get(key)
        if vp is not None and not vp.is_empty:
            return {"cards": [render_summary_card(vp)]}
        _enqueue_generate(state, key, body["context"], body.get("prefetch", {}))
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
        vp = state.cache.get(key)
        if vp is None:
            _enqueue_generate(state, key, context, body.get("prefetch", {}))
            return {"cards": []}
        cards = render_suggestion_cards(vp, state.index, context["patientId"],
                                        exclude_codes=_draft_codes(context))
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
        vp = state.cache.get(cache_key(context))
        if vp is None:
            return {"cards": []}
        card = render_missing_items_card(vp, state.index, _draft_codes(context))
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
