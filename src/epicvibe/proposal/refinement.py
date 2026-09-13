"""Clinician-refined proposals handed back from the SMART app.

Inside Epic a SMART app has no write channel for orders: a direct
ServiceRequest/MedicationRequest create is refused outside a CDS Hooks
interaction, and there is no SMART Web Messaging.  What *does* file an order is
an accepted CDS card `create` suggestion.  So the SMART app hands its refined
selections back to this service, and the next `order-select` / `order-sign`
hook for that encounter emits them as create actions.

See `docs/spikes/fhir-order-writeback.md` for the spike that established this.
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from epicvibe.catalog.index import CatalogIndex
from epicvibe.proposal.schema import Proposal, ProposedItem
from epicvibe.proposal.validation import ValidatedProposal, validate_proposal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RefinedProposal(BaseModel):
    """What the clinician actually chose, plus the AI proposal it came from."""

    refined: ValidatedProposal
    original: Proposal | None = None
    # Marker so a hook (and the audit log) can tell a reviewed set apart from a
    # raw AI proposal.  Always true for anything this module builds.
    refined_by_user: bool = True
    refined_at: str = ""
    encounter_id: str = ""
    patient_id: str = ""

    @property
    def selected_count(self) -> int:
        return sum(1 for o in self.refined.proposal.order_sets for i in o.items if i.include)


def _original_items(original: ValidatedProposal | None) -> dict[str, tuple[str, ProposedItem]]:
    out: dict[str, tuple[str, ProposedItem]] = {}
    if original is None:
        return out
    for osp in original.proposal.order_sets:
        for pi in osp.items:
            out[pi.item_id] = (osp.order_set_id, pi)
    return out


def build_refinement(index: CatalogIndex, items: list[Any], *,
                     original: ValidatedProposal | None = None,
                     patient_id: str = "", encounter_id: str = "",
                     ) -> tuple[RefinedProposal, list[dict]]:
    """Validate the clinician's edited selections against the catalog.

    Returns the refined proposal plus the entries that could not be grounded.
    Selected items carry `include=True` and the clinician's edited values in
    `prepopulated`; anything the AI proposed but the clinician deselected is
    kept with `include=False` so the stored set is a full audit picture.
    """
    originals = _original_items(original)
    failed: list[dict] = []
    by_set: dict[str, list[dict]] = {}
    seen: set[str] = set()

    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("item_id", ""))
        found = index.get_item(item_id)
        if found is None:
            failed.append({"item_id": item_id, "error": "not in catalog"})
            continue
        owner, item = found
        variant_id = str(entry.get("variant_id", ""))
        if index.get_variant(item.item_id, variant_id) is None:
            failed.append({"item_id": item.item_id, "error": "variant not in catalog"})
            continue
        order_set_id = str(entry.get("order_set_id") or "") or owner.order_set_id
        fields = entry.get("fields") or {}
        prepopulated = ({str(k).strip().lower(): str(v) for k, v in fields.items()
                         if v not in (None, "")} if isinstance(fields, dict) else {})
        _, prior = originals.get(item.item_id, ("", None))
        by_set.setdefault(order_set_id, []).append({
            "item_id": item.item_id,
            "variant_id": variant_id,
            "include": True,
            "rationale": (prior.rationale if prior else "") or "Selected in the Order Assistant",
            "parameter_recommendations": [],
            "prepopulated": prepopulated,
            "evidence": list(prior.evidence) if prior else [],
        })
        seen.add(item.item_id)

    # Deselected AI suggestions ride along with include=False (audit / diff).
    for item_id, (order_set_id, pi) in originals.items():
        if item_id in seen:
            continue
        dropped = pi.model_dump()
        dropped["include"] = False
        by_set.setdefault(order_set_id, []).append(dropped)

    raw = {"order_sets": [{"order_set_id": osid,
                           "rationale": _set_rationale(original, osid),
                           "items": entries}
                          for osid, entries in by_set.items()],
           "confidence": "high" if seen else "low"}
    refined = validate_proposal(raw, index)
    return (RefinedProposal(refined=refined,
                            original=original.proposal if original else None,
                            refined_at=_now_iso(),
                            encounter_id=encounter_id, patient_id=patient_id),
            failed)


def _set_rationale(original: ValidatedProposal | None, order_set_id: str) -> str:
    if original is not None:
        for osp in original.proposal.order_sets:
            if osp.order_set_id == order_set_id:
                return osp.rationale or "Reviewed in the EpicVibe Order Assistant"
    return "Reviewed in the EpicVibe Order Assistant"
