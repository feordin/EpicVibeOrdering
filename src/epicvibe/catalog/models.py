from typing import Literal

from pydantic import BaseModel, ConfigDict

PREF_LIST_SYSTEM = "urn:com.epic.cdshooks.action.code.system.preference-list-item"
ORDERSET_SYSTEM = "urn:com.epic.cdshooks.action.code.system.orderset-item"


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="allow")


class ItemVariant(_Tolerant):
    variant_id: str
    code_system: str = PREF_LIST_SYSTEM
    code: str
    display: str
    defaults: dict[str, str] = {}


class OrderItem(_Tolerant):
    item_id: str
    name: str
    order_type: Literal["medication", "procedure"]
    default_selected: bool = False
    variants: list[ItemVariant]


class OrderGroup(_Tolerant):
    group_id: str
    name: str
    items: list[OrderItem] = []


class OrderSet(_Tolerant):
    order_set_id: str
    name: str
    keywords: list[str] = []
    icd10_codes: list[str] = []
    groups: list[OrderGroup] = []


class CatalogMeta(_Tolerant):
    epic_version: str = "unknown"
    extracted_at: str = "unknown"
    site_id: str = "unknown"


class Catalog(_Tolerant):
    meta: CatalogMeta = CatalogMeta()
    order_sets: list[OrderSet] = []
