import json
from pathlib import Path
from epicvibe.catalog.models import Catalog

FIXTURE = Path("fixtures/catalog/sample_catalog.json")

def test_sample_catalog_validates():
    cat = Catalog.model_validate(json.loads(FIXTURE.read_text()))
    assert cat.meta.site_id == "CUSTOMER1"
    dm = cat.order_sets[0]
    assert dm.order_set_id == "AMB_DM2_NEWDX"
    met = [i for g in dm.groups for i in g.items if i.item_id == "ITEM_METFORMIN"][0]
    assert met.order_type == "medication"
    assert {v.variant_id for v in met.variants} == {"V_MET_500", "V_MET_1000"}

def test_unknown_fields_tolerated():
    cat = Catalog.model_validate({"meta": {"site_custom": "x"}, "order_sets": []})
    assert cat.order_sets == []
