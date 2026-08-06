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


class OrderSetProposal(BaseModel):
    order_set_id: str
    rationale: str
    items: list[ProposedItem]


class Proposal(BaseModel):
    order_sets: list[OrderSetProposal]
    confidence: Literal["high", "medium", "low"]


def proposal_json_schema() -> dict:
    return Proposal.model_json_schema()
