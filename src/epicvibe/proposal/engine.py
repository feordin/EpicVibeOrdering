import json
import logging

from epicvibe.catalog.index import CatalogIndex
from epicvibe.inference.base import InferenceProvider
from epicvibe.proposal.patient_summary import PatientSummary
from epicvibe.proposal.schema import Proposal, proposal_json_schema
from epicvibe.proposal.validation import ValidatedProposal, validate_proposal

log = logging.getLogger("epicvibe.proposal")

SYSTEM_PROMPT = """You are an order-set selection assistant for clinicians.

You receive (a) a de-identified patient summary assembled from every source the
EHR exposed -- problem list, active medications, allergies, vitals and labs,
encounter context and already-placed orders -- and (b) slices of this
institution's approved order-set catalog.

Your job: select the applicable order set(s), decide which component items to
include or exclude, choose the most clinically appropriate pre-built variant of
each item, and PRE-POPULATE each included item as completely as the patient data
allows so the clinician has little left to type.

Rules:
- Propose ONLY order sets, items, and variants present in the provided catalog.
  Never invent an order_set_id, item_id or variant_id.
- Give a short clinical rationale per item.
- `prepopulated` is a flat map of concrete order-entry values. Use the field
  names the order type implies:
    medications -> dose, route, frequency, duration, indication
    labs/imaging -> priority, occurrence, specimen, note
    referrals/consults -> reason, priority, note
  Values must be concrete and enterable ("1 g", "IV", "q24h", "STAT",
  "2026-09-13", "renal dosing: eGFR 48"), never advice prose.
- `evidence` cites the specific summary data you used, one short string each,
  e.g. "A1c 8.4% (2026-08-30)", "eGFR 48 mL/min", "penicillin allergy - rash".
- Respect allergies, renal function and the existing medication list: exclude or
  re-dose items that conflict, and say so in the rationale.
- Do not re-order anything already present in active_orders or medications.
- `parameter_recommendations` remain advisory display text; they are never
  applied automatically.
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
        if summary.encounter:
            for reason in summary.encounter.reasons + summary.encounter.type:
                keywords.update(reason.lower().split())
        shortlist = self.index.shortlist(codes, keywords)
        if not shortlist:
            return _empty()
        user = json.dumps({
            # Direct identifiers are never sent: patient_id/encounter_id are
            # excluded and the summary itself carries no name, MRN or DOB.
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
