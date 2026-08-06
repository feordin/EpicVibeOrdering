from epicvibe.catalog.models import Catalog, ItemVariant, OrderItem, OrderSet


class CatalogIndex:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._sets: dict[str, OrderSet] = {o.order_set_id: o for o in catalog.order_sets}
        self._items: dict[str, tuple[OrderSet, OrderItem]] = {}
        self._variants: dict[tuple[str, str], ItemVariant] = {}
        for oset in catalog.order_sets:
            for group in oset.groups:
                for item in group.items:
                    self._items[item.item_id] = (oset, item)
                    for v in item.variants:
                        self._variants[(item.item_id, v.variant_id)] = v

    def get_order_set(self, order_set_id: str) -> OrderSet | None:
        return self._sets.get(order_set_id)

    def get_item(self, item_id: str) -> tuple[OrderSet, OrderItem] | None:
        return self._items.get(item_id)

    def get_variant(self, item_id: str, variant_id: str) -> ItemVariant | None:
        return self._variants.get((item_id, variant_id))

    def shortlist(self, icd10_codes: set[str], keywords: set[str]) -> list[OrderSet]:
        codes = {c.upper() for c in icd10_codes}
        words = {k.lower() for k in keywords}
        hits = []
        for oset in self.catalog.order_sets:
            prefixes = [p.upper() for p in oset.icd10_codes]
            code_hit = any(c.startswith(p) for c in codes for p in prefixes)
            haystack = " ".join([oset.name.lower(), *[k.lower() for k in oset.keywords]])
            word_hit = any(w in haystack for w in words)
            if code_hit or word_hit:
                hits.append(oset)
        return hits
