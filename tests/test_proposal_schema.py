import pytest
from pydantic import ValidationError
from epicvibe.proposal.schema import Proposal, proposal_json_schema

def test_roundtrip():
    raw = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx",
            "items": [{"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True,
                       "rationale": "no A1c in 9 months"}]}], "confidence": "high"}
    p = Proposal.model_validate(raw)
    assert p.order_sets[0].items[0].parameter_recommendations == []

def test_rejects_bad_confidence():
    with pytest.raises(ValidationError):
        Proposal.model_validate({"order_sets": [], "confidence": "certain"})

def test_json_schema_exports():
    schema = proposal_json_schema()
    assert schema["type"] == "object" and "order_sets" in schema["properties"]
