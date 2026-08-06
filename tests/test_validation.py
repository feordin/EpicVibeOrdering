from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.proposal.validation import validate_proposal

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

def _item(item_id="ITEM_A1C", variant_id="V_A1C"):
    return {"item_id": item_id, "variant_id": variant_id, "include": True, "rationale": "r"}

def _raw(items):
    return {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "r", "items": items}],
            "confidence": "high"}

def test_valid_passes_clean():
    vp = validate_proposal(_raw([_item()]), IDX)
    assert vp.violations == [] and not vp.is_empty

def test_unknown_item_dropped():
    vp = validate_proposal(_raw([_item(), _item(item_id="ITEM_FAKE")]), IDX)
    assert len(vp.violations) == 1 and vp.violations[0].kind == "item"
    assert len(vp.proposal.order_sets[0].items) == 1

def test_unknown_variant_dropped():
    vp = validate_proposal(_raw([_item(variant_id="V_FAKE")]), IDX)
    assert vp.violations[0].kind == "variant" and vp.is_empty

def test_wrong_set_membership_dropped():
    vp = validate_proposal(_raw([_item(item_id="ITEM_BMP", variant_id="V_BMP")]), IDX)
    assert vp.violations[0].kind == "membership"

def test_unknown_order_set_dropped():
    raw = {"order_sets": [{"order_set_id": "NOPE", "rationale": "r", "items": [_item()]}],
           "confidence": "high"}
    vp = validate_proposal(raw, IDX)
    assert vp.violations[0].kind == "order_set" and vp.is_empty

def test_schema_garbage():
    vp = validate_proposal({"nope": 1}, IDX)
    assert vp.violations[0].kind == "schema" and vp.is_empty
    assert vp.proposal.confidence == "low"

def test_grounding_emptied_proposal_forces_low_confidence():
    raw = {"order_sets": [{"order_set_id": "NOPE", "rationale": "r", "items": [_item()]}],
           "confidence": "high"}
    vp = validate_proposal(raw, IDX)
    assert vp.is_empty and vp.proposal.confidence == "low"
