from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.inference.base import FakeProvider
from epicvibe.proposal.engine import ProposalEngine
from epicvibe.proposal.patient_summary import CodedItem, PatientSummary

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

def _summary():
    return PatientSummary(patient_id="PAT1", encounter_id="ENC1",
        conditions=[CodedItem(code="E11.9", system="icd10", display="Type 2 diabetes")])

GOOD = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx",
        "items": [{"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "r"}]}],
        "confidence": "high"}

async def test_generates_validated_proposal():
    provider = FakeProvider(GOOD)
    vp = await ProposalEngine(IDX, provider).generate(_summary())
    assert not vp.is_empty and vp.violations == []
    assert "AMB_DM2_NEWDX" in provider.calls[0]["user"]      # shortlist in prompt
    assert "AMB_HTN" not in provider.calls[0]["user"]        # non-matching set excluded

async def test_no_shortlist_skips_llm():
    provider = FakeProvider(GOOD)
    summary = PatientSummary(patient_id="PAT1", conditions=[CodedItem(code="Z00.0")])
    vp = await ProposalEngine(IDX, provider).generate(summary)
    assert vp.is_empty and provider.calls == []

async def test_provider_error_is_soft():
    class Boom:
        async def complete_json(self, **kw):
            raise RuntimeError("api down")
    vp = await ProposalEngine(IDX, Boom()).generate(_summary())
    assert vp.is_empty
