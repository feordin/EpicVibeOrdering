import json
import logging
from pathlib import Path

from pydantic import ValidationError

from epicvibe.catalog.models import Catalog

log = logging.getLogger("epicvibe.catalog")


def load_catalog(path: Path) -> Catalog:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"catalog file is not valid JSON: {path}") from e
    try:
        catalog = Catalog.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"catalog file failed schema validation: {path}") from e
    for oset in catalog.order_sets:
        if oset.model_extra:
            log.warning("order set %s: ignoring unknown fields %s",
                        oset.order_set_id, sorted(oset.model_extra))
    return catalog
