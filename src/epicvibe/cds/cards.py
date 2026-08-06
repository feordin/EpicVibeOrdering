from uuid import uuid4

from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.models import ItemVariant, OrderItem
from epicvibe.proposal.validation import ValidatedProposal

SOURCE = {"label": "EpicVibe Ordering"}


def _resource_stub(item: OrderItem, variant: ItemVariant, patient_id: str) -> dict:
    coding = {"system": variant.code_system, "code": variant.code, "display": variant.display}
    subject = {"reference": f"Patient/{patient_id}"}
    if item.order_type == "medication":
        return {"resourceType": "MedicationRequest", "status": "draft", "intent": "proposal",
                "medicationCodeableConcept": {"coding": [coding]}, "subject": subject}
    return {"resourceType": "ServiceRequest", "status": "draft", "intent": "proposal",
            "code": {"coding": [coding]}, "subject": subject}


def _included(vp: ValidatedProposal):
    for osp in vp.proposal.order_sets:
        for pi in osp.items:
            if pi.include:
                yield osp, pi


def render_suggestion_cards(vp: ValidatedProposal, index: CatalogIndex, patient_id: str,
                            exclude_codes: frozenset[str] = frozenset()) -> list[dict]:
    cards = []
    for osp in vp.proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        suggestions, detail = [], [osp.rationale, ""]
        for pi in osp.items:
            if not pi.include:
                continue
            _, item = index.get_item(pi.item_id)
            variant = index.get_variant(pi.item_id, pi.variant_id)
            if variant.code in exclude_codes:
                continue
            suggestions.append({
                "uuid": str(uuid4()), "label": variant.display, "isRecommended": True,
                "actions": [{"type": "create", "description": pi.rationale,
                             "resource": _resource_stub(item, variant, patient_id)}]})
            recs = "; ".join(f"{r.label}: {r.value}" for r in pi.parameter_recommendations)
            line = f"- **{variant.display}** — {pi.rationale}"
            detail.append(line + (f" _(suggested: {recs})_" if recs else ""))
        if suggestions:
            cards.append({
                "summary": f"{oset.name}: {len(suggestions)} suggested orders"[:140],
                "detail": "\n".join(detail), "indicator": "info", "source": SOURCE,
                "selectionBehavior": "any", "suggestions": suggestions})
    return cards


def render_summary_card(vp: ValidatedProposal) -> dict:
    n = sum(1 for _ in _included(vp))
    names = ", ".join(o.order_set_id for o in vp.proposal.order_sets) or "none"
    return {"summary": f"AI order review ready: {n} suggested orders"[:140],
            "detail": f"Matched order sets: {names}. Suggestions will appear when ordering.",
            "indicator": "info", "source": SOURCE}


def render_missing_items_card(vp: ValidatedProposal, index: CatalogIndex,
                              draft_codes: frozenset[str]) -> dict | None:
    missing = []
    for _, pi in _included(vp):
        variant = index.get_variant(pi.item_id, pi.variant_id)
        if variant.code not in draft_codes:
            missing.append(f"- {variant.display}")
    if not missing:
        return None
    return {"summary": "Protocol items not yet ordered"[:140],
            "detail": "\n".join(missing), "indicator": "info", "source": SOURCE}
