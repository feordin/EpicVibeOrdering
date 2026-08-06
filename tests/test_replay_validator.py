import copy

from epicvibe.catalog.models import PREF_LIST_SYSTEM
from epicvibe.replay.validator import validate_epic_response

GOOD_RESPONSE = {
    "cards": [
        {
            "summary": "Diabetes Mellitus Type 2 - New Diagnosis: 1 suggested orders",
            "detail": "New dx",
            "indicator": "info",
            "source": {"label": "EpicVibe Ordering"},
            "selectionBehavior": "any",
            "suggestions": [
                {
                    "uuid": "u1",
                    "label": "Hemoglobin A1c",
                    "isRecommended": True,
                    "actions": [
                        {
                            "type": "create",
                            "description": "Baseline glycemic assessment.",
                            "resource": {
                                "resourceType": "ServiceRequest",
                                "status": "draft",
                                "intent": "proposal",
                                "code": {"coding": [{"system": PREF_LIST_SYSTEM,
                                                     "code": "PREF_A1C",
                                                     "display": "Hemoglobin A1c"}]},
                                "subject": {"reference": "Patient/PAT1"},
                            },
                        }
                    ],
                }
            ],
        }
    ]
}


def test_good_response_has_no_violations():
    assert validate_epic_response(copy.deepcopy(GOOD_RESPONSE), "order-select") == []


def test_unknown_top_level_key_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["extra"] = "nope"
    violations = validate_epic_response(resp, "order-select")
    assert any("unknown top-level key" in v.lower() for v in violations)


def test_cards_missing_violates():
    resp = {}
    violations = validate_epic_response(resp, "patient-view")
    assert any("cards" in v.lower() for v in violations)


def test_cards_not_a_list_violates():
    resp = {"cards": "nope"}
    violations = validate_epic_response(resp, "patient-view")
    assert any("cards" in v.lower() for v in violations)


def test_system_actions_present_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["systemActions"] = [{"type": "create"}]
    violations = validate_epic_response(resp, "order-select")
    assert any("systemActions" in v and "ServiceRequest.Update" in v for v in violations)


def test_summary_too_long_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["summary"] = "x" * 141
    violations = validate_epic_response(resp, "order-select")
    assert any("summary" in v.lower() for v in violations)


def test_bad_indicator_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["indicator"] = "urgent"
    violations = validate_epic_response(resp, "order-select")
    assert any("indicator" in v.lower() for v in violations)


def test_missing_source_label_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["source"] = {}
    violations = validate_epic_response(resp, "order-select")
    assert any("source" in v.lower() for v in violations)


def test_selection_behavior_not_any_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["selectionBehavior"] = "at-most-one"
    violations = validate_epic_response(resp, "order-select")
    assert any("selectionBehavior" in v for v in violations)


def test_suggestion_missing_uuid_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["uuid"] = ""
    violations = validate_epic_response(resp, "order-select")
    assert any("uuid" in v.lower() for v in violations)


def test_suggestion_missing_label_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["label"] = ""
    violations = validate_epic_response(resp, "order-select")
    assert any("label" in v.lower() for v in violations)


def test_suggestion_two_actions_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    action = resp["cards"][0]["suggestions"][0]["actions"][0]
    resp["cards"][0]["suggestions"][0]["actions"] = [action, copy.deepcopy(action)]
    violations = validate_epic_response(resp, "order-select")
    assert any("exactly one" in v.lower() for v in violations)


def test_action_bad_type_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0]["type"] = "update"
    violations = validate_epic_response(resp, "order-select")
    assert any("type" in v.lower() for v in violations)


def test_action_bad_resource_type_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0]["resource"]["resourceType"] = "CarePlan"
    violations = validate_epic_response(resp, "order-select")
    assert any("resourceType" in v for v in violations)


def test_action_bad_subject_reference_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0]["resource"]["subject"] = {"reference": "Group/G1"}
    violations = validate_epic_response(resp, "order-select")
    assert any("subject" in v.lower() for v in violations)


def test_action_missing_status_intent_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    del resp["cards"][0]["suggestions"][0]["actions"][0]["resource"]["status"]
    violations = validate_epic_response(resp, "order-select")
    assert any("status" in v.lower() for v in violations)


def test_action_empty_coding_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0]["resource"]["code"]["coding"] = []
    violations = validate_epic_response(resp, "order-select")
    assert any("coding" in v.lower() for v in violations)


def test_action_unknown_coding_system_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0]["resource"]["code"]["coding"][0]["system"] = "urn:bogus"
    violations = validate_epic_response(resp, "order-select")
    assert any("coding" in v.lower() and "system" in v.lower() for v in violations)


def test_link_bad_type_violates():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["links"] = [{"label": "l", "url": "http://x", "type": "html"}]
    violations = validate_epic_response(resp, "order-select")
    assert any("link" in v.lower() for v in violations)


def test_delete_action_does_not_require_resource():
    resp = copy.deepcopy(GOOD_RESPONSE)
    resp["cards"][0]["suggestions"][0]["actions"][0] = {"type": "delete", "description": "remove"}
    violations = validate_epic_response(resp, "order-select")
    assert violations == []
