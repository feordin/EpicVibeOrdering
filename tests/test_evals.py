from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.proposal.validation import validate_proposal
from evals.scoring import Expected, score

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
EXPECTED = Expected(order_set_id="AMB_DM2_NEWDX",
                    required_items=["ITEM_A1C", "ITEM_METFORMIN"])

def _vp(items):
    return validate_proposal({"order_sets": [{"order_set_id": "AMB_DM2_NEWDX",
        "rationale": "r", "items": items}], "confidence": "high"}, IDX)

def _item(item_id, variant_id):
    return {"item_id": item_id, "variant_id": variant_id, "include": True, "rationale": "r"}

def test_perfect_score():
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_METFORMIN", "V_MET_500")]),
              EXPECTED, "t")
    assert s.grounded and s.recall == 1.0 and s.precision == 1.0

def test_partial_recall():
    s = score(_vp([_item("ITEM_A1C", "V_A1C")]), EXPECTED, "t")
    assert s.recall == 0.5 and s.precision == 1.0

def test_forbidden_hurts_precision():
    exp = Expected(order_set_id="AMB_DM2_NEWDX", required_items=["ITEM_A1C"],
                   forbidden_items=["ITEM_RETINAL"])
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_RETINAL", "V_RETINAL")]), exp, "t")
    assert s.precision == 0.5

def test_ungrounded_flagged():
    s = score(_vp([_item("ITEM_FAKE", "V_FAKE")]), EXPECTED, "t")
    assert s.grounded is False
