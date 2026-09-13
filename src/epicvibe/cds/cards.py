"""CDS Hooks card rendering.

Cards carry two complementary affordances:

* `suggestions[].actions[]` -- one `create` action per suggestion (Epic allows
  exactly one action per suggestion), with the draft FHIR resource
  pre-populated as far as FHIR lets us express the proposal's values.
* `links[]` -- a `smart` link into the EpicVibe SMART on FHIR app, where the
  clinician sees the fully pre-populated order set and can send it to the EHR.

Both are part of CDS Hooks 2.0: a card may carry `suggestions` and `links`
simultaneously, and `links[].type` of `smart` is the spec's SMART-app launch.
"""

import re
from datetime import datetime, timezone
from uuid import uuid4

from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.models import ItemVariant, OrderItem
from epicvibe.proposal.schema import ProposedItem
from epicvibe.proposal.validation import ValidatedProposal

SOURCE = {"label": "EpicVibe Ordering"}
DEFAULT_SMART_LAUNCH_URL = "http://localhost:8000/smart/launch"
SMART_LINK_LABEL = "Open EpicVibe Order Assistant"

_QUANTITY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z%/]+.*?)\s*$")

# Common sig abbreviations -> FHIR Timing.repeat
_TIMING = {
    "qd": (1, 1, "d"), "daily": (1, 1, "d"), "q24h": (1, 24, "h"),
    "bid": (2, 1, "d"), "q12h": (2, 1, "d"),
    "tid": (3, 1, "d"), "q8h": (3, 1, "d"),
    "qid": (4, 1, "d"), "q6h": (4, 1, "d"),
    "q4h": (6, 1, "d"), "qhs": (1, 1, "d"), "weekly": (1, 1, "wk"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _prepop(pi: ProposedItem | None, variant: ItemVariant) -> dict[str, str]:
    """Variant defaults overlaid with the proposal's concrete values."""
    values: dict[str, str] = {k: str(v) for k, v in (variant.defaults or {}).items()}
    if pi is not None:
        for rec in pi.parameter_recommendations:
            values.setdefault(rec.label.strip().lower(), rec.value)
        values.update({k.strip().lower(): str(v) for k, v in (pi.prepopulated or {}).items()})
    return values


def _quantity(text: str) -> dict | None:
    match = _QUANTITY_RE.match(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2).strip()
    return {"value": int(value) if value.is_integer() else value, "unit": unit}


def _timing(frequency: str) -> dict | None:
    key = (frequency or "").strip().lower()
    spec = _TIMING.get(key)
    if spec is None:
        return None
    freq, period, unit = spec
    return {"repeat": {"frequency": freq, "period": period, "periodUnit": unit}}


def _sig_text(display: str, values: dict[str, str]) -> str:
    parts = [values.get("dose", ""), values.get("route", ""), values.get("frequency", "")]
    sig = " ".join(p for p in parts if p).strip()
    if values.get("duration"):
        sig = f"{sig} for {values['duration']}".strip()
    return sig or display


def _reason_code(values: dict[str, str], reason_concepts: list[dict] | None) -> list[dict]:
    text = values.get("reason") or values.get("indication") or ""
    if text:
        return [{"text": text}]
    if reason_concepts:
        return [c for c in reason_concepts[:1] if c]
    return []


def build_order_resource(item: OrderItem, variant: ItemVariant, patient_id: str,
                         pi: ProposedItem | None = None, *,
                         encounter_id: str | None = None,
                         user_id: str | None = None,
                         reason_concepts: list[dict] | None = None,
                         status: str = "draft", intent: str = "proposal",
                         overrides: dict[str, str] | None = None) -> dict:
    """Draft FHIR resource for one proposed order, pre-populated where FHIR allows."""
    values = _prepop(pi, variant)
    if overrides:
        values.update({k.strip().lower(): str(v) for k, v in overrides.items() if v not in (None, "")})

    coding = {"system": variant.code_system, "code": variant.code, "display": variant.display}
    resource: dict = {
        "resourceType": "MedicationRequest" if item.order_type == "medication" else "ServiceRequest",
        "status": status, "intent": intent,
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": _now_iso(),
    }
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    if user_id:
        reference = user_id if "/" in user_id else f"Practitioner/{user_id}"
        resource["requester"] = {"reference": reference}

    reason = _reason_code(values, reason_concepts)

    if item.order_type == "medication":
        resource["medicationCodeableConcept"] = {"coding": [coding], "text": variant.display}
        dosage: dict = {"text": _sig_text(variant.display, values)}
        timing = _timing(values.get("frequency", ""))
        if timing:
            dosage["timing"] = timing
        if values.get("route"):
            dosage["route"] = {"text": values["route"]}
        dose_qty = _quantity(values.get("dose", ""))
        if dose_qty:
            dosage["doseAndRate"] = [{"doseQuantity": dose_qty}]
        resource["dosageInstruction"] = [dosage]
        if reason:
            resource["reasonCode"] = reason
        if values.get("priority"):
            resource["priority"] = _fhir_priority(values["priority"])
        if values.get("note"):
            resource["note"] = [{"text": values["note"]}]
        return resource

    resource["code"] = {"coding": [coding], "text": variant.display}
    if values.get("priority"):
        resource["priority"] = _fhir_priority(values["priority"])
    if reason:
        resource["reasonCode"] = reason
    if values.get("occurrence"):
        resource["occurrenceDateTime"] = values["occurrence"]
    notes = [values["note"]] if values.get("note") else []
    notes += [f"{k}: {values[k]}" for k in ("specimen", "frequency") if values.get(k)]
    if notes:
        resource["note"] = [{"text": "; ".join(notes)}]
    return resource


def _fhir_priority(value: str) -> str:
    mapping = {"stat": "stat", "urgent": "urgent", "asap": "asap",
               "routine": "routine", "now": "stat", "timed": "routine"}
    return mapping.get(value.strip().lower(), "routine")


# Backwards-compatible alias (older call sites / tests).
def _resource_stub(item: OrderItem, variant: ItemVariant, patient_id: str) -> dict:
    return build_order_resource(item, variant, patient_id)


def _included(vp: ValidatedProposal):
    for osp in vp.proposal.order_sets:
        for pi in osp.items:
            if pi.include:
                yield osp, pi


def smart_link(smart_launch_url: str = DEFAULT_SMART_LAUNCH_URL) -> dict:
    return {"label": SMART_LINK_LABEL, "url": smart_launch_url, "type": "smart"}


def render_suggestion_cards(vp: ValidatedProposal, index: CatalogIndex, patient_id: str,
                            exclude_codes: frozenset[str] = frozenset(), *,
                            encounter_id: str | None = None,
                            user_id: str | None = None,
                            reason_concepts: list[dict] | None = None,
                            smart_launch_url: str = DEFAULT_SMART_LAUNCH_URL,
                            reviewed: bool = False) -> list[dict]:
    """`reviewed=True` renders a clinician-refined set handed back from the
    SMART app: the summary says so, and the detail always shows the concrete
    (clinician-edited) values rather than the AI's parameter recommendations."""
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
            resource = build_order_resource(
                item, variant, patient_id, pi, encounter_id=encounter_id,
                user_id=user_id, reason_concepts=reason_concepts)
            suggestions.append({
                "uuid": str(uuid4()), "label": variant.display, "isRecommended": True,
                "actions": [{"type": "create", "description": pi.rationale,
                             "resource": resource}]})
            values = _prepop(pi, variant)
            recs = "" if reviewed else "; ".join(
                f"{r.label}: {r.value}" for r in pi.parameter_recommendations)
            if not recs:
                recs = "; ".join(f"{k}: {v}" for k, v in values.items())
            line = f"- **{variant.display}** — {pi.rationale}"
            if recs:
                line += f" _({'reviewed' if reviewed else 'suggested'}: {recs})_"
            if pi.evidence:
                line += f" _[{'; '.join(pi.evidence)}]_"
            detail.append(line)
        if suggestions:
            summary = (f"Reviewed in Order Assistant: {len(suggestions)} selected orders"
                       if reviewed else f"{oset.name}: {len(suggestions)} suggested orders")
            cards.append({
                "uuid": str(uuid4()),
                "summary": summary[:140],
                "detail": "\n".join(detail), "indicator": "info", "source": SOURCE,
                "selectionBehavior": "any", "suggestions": suggestions,
                "links": [smart_link(smart_launch_url)]})
    return cards


def render_summary_card(vp: ValidatedProposal, index: CatalogIndex,
                        smart_launch_url: str = DEFAULT_SMART_LAUNCH_URL,
                        reviewed: bool = False) -> dict:
    n = sum(1 for _ in _included(vp))
    # Human-readable order-set names, never the ids: ids like `ED_CAP_ADMIT`
    # render as italics in the card's markdown detail.
    labels = []
    for osp in vp.proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        labels.append(oset.name if oset is not None else osp.order_set_id.replace("_", " "))
    names = ", ".join(labels) or "none"
    if reviewed:
        summary = f"Reviewed in Order Assistant: {n} selected orders"
        detail = (f"Matched order sets: {names}. A set reviewed in the EpicVibe Order "
                  "Assistant is waiting — open order entry and the reviewed orders will "
                  "appear as suggestions to accept and sign.")
    else:
        summary = f"AI order review ready: {n} suggested orders"
        detail = (f"Matched order sets: {names}. Suggestions will appear when ordering, "
                  "or open the EpicVibe Order Assistant to review the pre-populated set.")
    return {"uuid": str(uuid4()),
            "summary": summary[:140],
            "detail": detail,
            "indicator": "info", "source": SOURCE,
            "links": [smart_link(smart_launch_url)]}


def render_missing_items_card(vp: ValidatedProposal, index: CatalogIndex,
                              draft_codes: frozenset[str],
                              smart_launch_url: str = DEFAULT_SMART_LAUNCH_URL,
                              reviewed: bool = False) -> dict | None:
    missing = []
    for _, pi in _included(vp):
        variant = index.get_variant(pi.item_id, pi.variant_id)
        if variant.code not in draft_codes:
            values = _prepop(pi, variant)
            line = f"- {variant.display}"
            if reviewed and values:
                line += " _(" + "; ".join(f"{k}: {v}" for k, v in values.items()) + ")_"
            missing.append(line)
    if not missing:
        return None
    summary = (f"Reviewed in Order Assistant: {len(missing)} selected orders not yet placed"
               if reviewed else "Protocol items not yet ordered")
    return {"uuid": str(uuid4()),
            "summary": summary[:140],
            "detail": "\n".join(missing), "indicator": "info", "source": SOURCE,
            "links": [smart_link(smart_launch_url)]}
