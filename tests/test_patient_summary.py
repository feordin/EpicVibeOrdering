import json
from pathlib import Path
from epicvibe.proposal.patient_summary import summarize_prefetch

def _body():
    return json.loads(Path("fixtures/hooks/patient_view.json").read_text())

def test_summarize():
    body = _body()
    s = summarize_prefetch(body["context"], body["prefetch"])
    assert s.patient_id == "PAT1" and s.encounter_id == "ENC1"
    assert s.conditions[0].code == "E11.9"
    assert s.medications[0].display.startswith("lisinopril")

def test_missing_prefetch_is_soft():
    body = _body()
    s = summarize_prefetch(body["context"], {})
    assert s.conditions == [] and s.medications == []
    assert any("conditions" in n for n in s.notes)

def test_malformed_bundle_is_soft():
    body = _body()
    s = summarize_prefetch(body["context"], {"conditions": {"resourceType": "Bundle", "entry": "junk"}})
    assert s.conditions == []
