import logging
from typing import Literal

from pydantic import BaseModel, ValidationError

from epicvibe.catalog.index import CatalogIndex
from epicvibe.proposal.schema import OrderSetProposal, Proposal

log = logging.getLogger("epicvibe.proposal")


class GroundingViolation(BaseModel):
    kind: Literal["order_set", "item", "variant", "membership", "schema"]
    detail: str


class ValidatedProposal(BaseModel):
    proposal: Proposal
    violations: list[GroundingViolation] = []

    @property
    def is_empty(self) -> bool:
        return not any(i.include for o in self.proposal.order_sets for i in o.items)


def _empty() -> Proposal:
    return Proposal(order_sets=[], confidence="low")


def validate_proposal(raw: dict, index: CatalogIndex) -> ValidatedProposal:
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError as e:
        log.warning("proposal failed schema validation: %s", e.error_count())
        return ValidatedProposal(proposal=_empty(),
                                 violations=[GroundingViolation(kind="schema", detail=str(e.error_count()) + " schema errors")])

    violations: list[GroundingViolation] = []
    kept_sets: list[OrderSetProposal] = []
    for osp in proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        if oset is None:
            violations.append(GroundingViolation(kind="order_set", detail=osp.order_set_id))
            continue
        kept_items = []
        for pi in osp.items:
            found = index.get_item(pi.item_id)
            if found is None:
                violations.append(GroundingViolation(kind="item", detail=pi.item_id))
                continue
            owner, _ = found
            if owner.order_set_id != osp.order_set_id:
                violations.append(GroundingViolation(
                    kind="membership", detail=f"{pi.item_id} not in {osp.order_set_id}"))
                continue
            if index.get_variant(pi.item_id, pi.variant_id) is None:
                violations.append(GroundingViolation(
                    kind="variant", detail=f"{pi.item_id}/{pi.variant_id}"))
                continue
            kept_items.append(pi)
        kept_sets.append(OrderSetProposal(order_set_id=osp.order_set_id,
                                          rationale=osp.rationale, items=kept_items))
    for v in violations:
        log.warning("grounding violation [%s]: %s", v.kind, v.detail)
    return ValidatedProposal(
        proposal=Proposal(order_sets=kept_sets, confidence=proposal.confidence),
        violations=violations)
