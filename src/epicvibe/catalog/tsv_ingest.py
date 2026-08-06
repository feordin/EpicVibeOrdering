import logging
from pathlib import Path

import pandas as pd

from epicvibe.catalog.models import (Catalog, CatalogMeta, ItemVariant,
                                     OrderGroup, OrderItem, OrderSet)

log = logging.getLogger("epicvibe.catalog")

_SETS_COLS = {"ORDER_SET_ID", "NAME", "KEYWORDS", "ICD10_CODES"}
_LINES_COLS = {"ORDER_SET_ID", "GROUP_ID", "GROUP_NAME", "ITEM_ID", "ITEM_NAME",
               "ORDER_TYPE", "DEFAULT_SELECTED", "VARIANT_ID", "CODE", "DISPLAY",
               "DOSE", "ROUTE", "FREQUENCY"}
_DEFAULT_FIELDS = {"DOSE": "dose", "ROUTE": "route", "FREQUENCY": "frequency"}


def _read(path: Path, known: set[str]) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t", dtype=str).fillna("")
    unknown = set(df.columns) - known
    if unknown:
        log.warning("%s: ignoring unknown columns %s", path.name, sorted(unknown))
    return df


def ingest_tsv(extract_dir: Path) -> Catalog:
    sets_df = _read(extract_dir / "order_sets.tsv", _SETS_COLS)
    lines_df = _read(extract_dir / "order_set_lines.tsv", _LINES_COLS)

    order_sets = []
    for _, srow in sets_df.iterrows():
        os_id = srow["ORDER_SET_ID"]
        groups: dict[str, OrderGroup] = {}
        items: dict[str, OrderItem] = {}
        for _, row in lines_df[lines_df["ORDER_SET_ID"] == os_id].iterrows():
            group = groups.setdefault(
                row["GROUP_ID"],
                OrderGroup(group_id=row["GROUP_ID"], name=row["GROUP_NAME"], items=[]))
            item = items.get(row["ITEM_ID"])
            if item is None:
                item = OrderItem(item_id=row["ITEM_ID"], name=row["ITEM_NAME"],
                                 order_type=row["ORDER_TYPE"],
                                 default_selected=row["DEFAULT_SELECTED"] == "Y",
                                 variants=[])
                items[row["ITEM_ID"]] = item
                group.items.append(item)
            defaults = {out: row[col] for col, out in _DEFAULT_FIELDS.items()
                        if col in row.index and row[col]}
            item.variants.append(ItemVariant(variant_id=row["VARIANT_ID"],
                                             code=row["CODE"], display=row["DISPLAY"],
                                             defaults=defaults))
        order_sets.append(OrderSet(
            order_set_id=os_id, name=srow["NAME"],
            keywords=[k for k in srow.get("KEYWORDS", "").split(";") if k],
            icd10_codes=[c for c in srow.get("ICD10_CODES", "").split(";") if c],
            groups=list(groups.values())))
    return Catalog(meta=CatalogMeta(), order_sets=order_sets)
