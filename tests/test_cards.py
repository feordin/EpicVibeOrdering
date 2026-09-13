from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                render_summary_card)
from epicvibe.proposal.validation import validate_proposal

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

RAW = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "New T2DM diagnosis",
    "items": [
        {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "baseline"},
        {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True,
         "rationale": "first-line",
         "parameter_recommendations": [{"label": "dose", "value": "500 mg BID", "rationale": "start low"}]},
        {"item_id": "ITEM_RETINAL", "variant_id": "V_RETINAL", "include": False, "rationale": "recent exam"}]}],
    "confidence": "high"}
VP = validate_proposal(RAW, IDX)

def test_suggestion_cards():
    cards = render_suggestion_cards(VP, IDX, "PAT1")
    assert len(cards) == 1
    card = cards[0]
    assert card["selectionBehavior"] == "any" and card["indicator"] == "info"
    assert len(card["suggestions"]) == 2                       # excluded item omitted
    med = [s for s in card["suggestions"] if "metFORMIN" in s["label"]][0]
    assert len(med["actions"]) == 1                            # one action per suggestion
    res = med["actions"][0]["resource"]
    assert res["resourceType"] == "MedicationRequest" and res["status"] == "draft"
    assert res["medicationCodeableConcept"]["coding"][0]["code"] == "PREF_MET_500"
    assert res["medicationCodeableConcept"]["text"]            # sandbox requires text alongside coding[0].code
    assert res["subject"]["reference"] == "Patient/PAT1"
    a1c = [s for s in card["suggestions"] if s["label"] == "Hemoglobin A1c"][0]
    a1c_res = a1c["actions"][0]["resource"]
    assert a1c_res["resourceType"] == "ServiceRequest"
    assert a1c_res["code"]["text"]                             # symmetry with medicationCodeableConcept.text
    assert "500 mg BID" in card["detail"]                      # advisory values in markdown

def test_exclude_codes():
    cards = render_suggestion_cards(VP, IDX, "PAT1", exclude_codes=frozenset({"PREF_A1C"}))
    assert len(cards[0]["suggestions"]) == 1

def test_summary_card():
    card = render_summary_card(VP, IDX)
    assert "suggestions" not in card and "2" in card["summary"]
    name = IDX.get_order_set("AMB_DM2_NEWDX").name
    assert name in card["detail"]
    assert "AMB_DM2_NEWDX" not in card["detail"]      # ids italicize in markdown
    assert "_" not in card["detail"]

def test_missing_items_card():
    card = render_missing_items_card(VP, IDX, frozenset({"PREF_MET_500"}))
    assert "Hemoglobin A1c" in card["detail"]
    assert render_missing_items_card(VP, IDX, frozenset({"PREF_MET_500", "PREF_A1C"})) is None
