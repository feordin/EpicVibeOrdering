"""Render a validated proposal as an editable, fully pre-populated order template."""

from epicvibe.catalog.index import CatalogIndex
from epicvibe.cds.cards import _prepop
from epicvibe.proposal.schema import ProposedItem
from epicvibe.proposal.validation import ValidatedProposal

# Field order shown in the UI, per order type.
MED_FIELDS = ["dose", "route", "frequency", "duration", "indication", "priority", "note"]
PROC_FIELDS = ["priority", "occurrence", "specimen", "frequency", "reason", "note"]


def _field_order(order_type: str) -> list[str]:
    return MED_FIELDS if order_type == "medication" else PROC_FIELDS


def _ordered(values: dict[str, str], order_type: str) -> list[dict]:
    keys = [k for k in _field_order(order_type) if k in values]
    keys += [k for k in values if k not in keys]
    return [{"name": k, "value": values[k]} for k in keys]


def render_template(vp: ValidatedProposal, index: CatalogIndex) -> list[dict]:
    """One entry per proposed order set, with every catalog group and item."""
    out = []
    for osp in vp.proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        if oset is None:
            continue
        proposed: dict[str, ProposedItem] = {pi.item_id: pi for pi in osp.items}
        groups = []
        for group in oset.groups:
            items = []
            for item in group.items:
                pi = proposed.get(item.item_id)
                variant = None
                if pi is not None:
                    variant = index.get_variant(item.item_id, pi.variant_id)
                if variant is None:
                    variant = item.variants[0] if item.variants else None
                if variant is None:
                    continue
                items.append({
                    "item_id": item.item_id,
                    "name": item.name,
                    "order_type": item.order_type,
                    "include": bool(pi.include) if pi else False,
                    "proposed": pi is not None,
                    "variant_id": variant.variant_id,
                    "variant_label": variant.display,
                    "variants": [{"variant_id": v.variant_id, "label": v.display}
                                 for v in item.variants],
                    "rationale": pi.rationale if pi else "",
                    "evidence": list(pi.evidence) if pi else [],
                    "fields": _ordered(_prepop(pi, variant), item.order_type),
                })
            if items:
                groups.append({"group_id": group.group_id, "name": group.name, "items": items})
        out.append({"order_set_id": oset.order_set_id, "name": oset.name,
                    "rationale": osp.rationale, "groups": groups})
    return out


def patient_banner(patient: dict | None) -> dict:
    """Display-only banner data.  This never reaches the LLM prompt."""
    if not isinstance(patient, dict):
        return {"name": "Unknown patient", "gender": "", "birth_date": "", "mrn": ""}
    name = ""
    for entry in patient.get("name") or []:
        if entry.get("text"):
            name = entry["text"]
            break
        given = " ".join(entry.get("given") or [])
        family = entry.get("family") or ""
        if given or family:
            name = f"{given} {family}".strip()
            break
    mrn = ""
    for ident in patient.get("identifier") or []:
        if ident.get("value"):
            mrn = ident["value"]
            break
    return {"name": name or "Unknown patient", "gender": patient.get("gender") or "",
            "birth_date": patient.get("birthDate") or "", "mrn": mrn}
