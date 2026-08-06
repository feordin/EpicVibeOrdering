from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog

def _index():
    return CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

def test_lookups():
    idx = _index()
    assert idx.get_order_set("AMB_DM2_NEWDX").name.startswith("Diabetes")
    oset, item = idx.get_item("ITEM_METFORMIN")
    assert oset.order_set_id == "AMB_DM2_NEWDX" and item.name == "Metformin"
    assert idx.get_variant("ITEM_METFORMIN", "V_MET_500").code == "PREF_MET_500"
    assert idx.get_variant("ITEM_METFORMIN", "NOPE") is None
    assert idx.get_item("NOPE") is None

def test_shortlist_by_icd10_prefix():
    hits = _index().shortlist({"E11.9"}, set())
    assert [o.order_set_id for o in hits] == ["AMB_DM2_NEWDX"]

def test_shortlist_by_keyword():
    hits = _index().shortlist(set(), {"hypertension"})
    assert [o.order_set_id for o in hits] == ["AMB_HTN"]

def test_shortlist_no_match():
    assert _index().shortlist({"Z00.0"}, {"sprain"}) == []
