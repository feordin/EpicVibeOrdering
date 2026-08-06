import logging
from pathlib import Path
from epicvibe.catalog.tsv_ingest import ingest_tsv

def test_ingest(caplog):
    with caplog.at_level(logging.WARNING, logger="epicvibe.catalog"):
        cat = ingest_tsv(Path("fixtures/extract"))
    assert "SITE_CUSTOM_COL" in caplog.text          # unknown column warned, not fatal
    oset = cat.order_sets[0]
    assert oset.order_set_id == "AMB_DM2_NEWDX"
    assert oset.icd10_codes == ["E11"]
    met = [i for g in oset.groups for i in g.items if i.item_id == "ITEM_METFORMIN"][0]
    assert len(met.variants) == 2
    assert met.variants[0].defaults["dose"] == "500 mg"
    a1c = [i for g in oset.groups for i in g.items if i.item_id == "ITEM_A1C"][0]
    assert a1c.default_selected is True and a1c.variants[0].defaults == {}
