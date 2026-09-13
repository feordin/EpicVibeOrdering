import json
import logging
from pathlib import Path
import pytest
from epicvibe.catalog.loader import load_catalog

def test_loads_sample():
    cat = load_catalog(Path("fixtures/catalog/sample_catalog.json"))
    assert {o.order_set_id for o in cat.order_sets} == {
        "AMB_DM2_NEWDX", "AMB_HTN", "ED_CAP_ADMIT", "IP_HF_EXACERBATION"}

def test_warns_on_unknown_keys(tmp_path, caplog):
    data = {"meta": {}, "order_sets": [{"order_set_id": "X", "name": "X", "CUSTOM_COL": 1, "groups": []}]}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING, logger="epicvibe.catalog"):
        load_catalog(p)
    assert "CUSTOM_COL" in caplog.text

def test_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        load_catalog(p)
