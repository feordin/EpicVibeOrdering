from pydantic import BaseModel

from epicvibe.proposal.validation import ValidatedProposal


class Expected(BaseModel):
    order_set_id: str
    required_items: list[str]
    forbidden_items: list[str] = []


class ScenarioScore(BaseModel):
    name: str
    grounded: bool
    recall: float
    precision: float


def score(vp: ValidatedProposal, expected: Expected, name: str) -> ScenarioScore:
    included = {pi.item_id for osp in vp.proposal.order_sets
                if osp.order_set_id == expected.order_set_id
                for pi in osp.items if pi.include}
    required = set(expected.required_items)
    forbidden = set(expected.forbidden_items)
    recall = len(included & required) / len(required) if required else 1.0
    precision = (len(included - forbidden) / len(included)) if included else 0.0
    return ScenarioScore(name=name, grounded=not vp.violations,
                         recall=recall, precision=precision)
