import json
import logging

from epicvibe.catalog.index import CatalogIndex
from epicvibe.inference.base import InferenceProvider
from epicvibe.proposal.patient_summary import PatientSummary
from epicvibe.proposal.schema import Proposal, proposal_json_schema
from epicvibe.proposal.validation import ValidatedProposal, validate_proposal

log = logging.getLogger("epicvibe.proposal")

SYSTEM_PROMPT = """You are an order-set selection assistant for ambulatory clinicians.
Given a patient summary and this institution's approved order-set catalog slices,
select the applicable order set(s), which component items to include or exclude,
and the most clinically appropriate pre-built variant of each item.
Rules:
- Propose ONLY order sets, items, and variants present in the provided catalog.
- Give a short clinical rationale per item.
- Parameter recommendations are advisory display text only; they are never applied.
- If nothing applies, return an empty order_sets list with confidence "low"."""


def _empty() -> ValidatedProposal:
    return ValidatedProposal(proposal=Proposal(order_sets=[], confidence="low"))


class ProposalEngine:
    def __init__(self, index: CatalogIndex, provider: InferenceProvider):
        self.index = index
        self.provider = provider

    async def generate(self, summary: PatientSummary) -> ValidatedProposal:
        codes = {c.code for c in summary.conditions if c.code}
        keywords = {w for c in summary.conditions for w in c.display.lower().split()}
        shortlist = self.index.shortlist(codes, keywords)
        if not shortlist:
            return _empty()
        user = json.dumps({
            "patient": summary.model_dump(exclude={"patient_id", "encounter_id"}),
            "catalog": [o.model_dump() for o in shortlist],
        })
        try:
            raw = await self.provider.complete_json(
                system=SYSTEM_PROMPT, user=user, json_schema=proposal_json_schema())
        except Exception:
            log.exception("inference call failed")
            return _empty()
        return validate_proposal(raw, self.index)
