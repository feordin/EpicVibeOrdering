from typing import Literal

from pydantic import BaseModel


class ParameterRecommendation(BaseModel):
    label: str
    value: str
    rationale: str


class ProposedItem(BaseModel):
    item_id: str
    variant_id: str
    include: bool
    rationale: str
    parameter_recommendations: list[ParameterRecommendation] = []
    # Concrete pre-population values keyed by field name
    # (dose, route, frequency, priority, occurrence, note, reason, specimen...).
    prepopulated: dict[str, str] = {}
    # Short citations of the patient data that drove this item, e.g.
    # "A1c 8.4% (2026-08-30)", "eGFR 48 mL/min".
    evidence: list[str] = []


class OrderSetProposal(BaseModel):
    order_set_id: str
    rationale: str
    items: list[ProposedItem]


class Proposal(BaseModel):
    order_sets: list[OrderSetProposal]
    confidence: Literal["high", "medium", "low"]


def proposal_json_schema() -> dict:
    return Proposal.model_json_schema()
