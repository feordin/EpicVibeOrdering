# Epic AI Ordering Prototype — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python backend that turns patient context into catalog-grounded order-set proposals and serves them as CDS Hooks suggestion cards, testable end-to-end against public sandboxes.

**Architecture:** FastAPI CDS Hooks service in front of a Proposal Engine. The engine calls a pluggable LLM endpoint with a strict JSON schema, validates every proposal against a catalog loaded from the customer's order-set extract, and caches results per encounter so hook handlers never wait on the LLM. SQLite audit store records proposals and provider feedback. Spec: `docs/superpowers/specs/2026-08-04-epic-ai-ordering-design.md`.

**Tech Stack:** Python 3.12+, FastAPI, pydantic v2 + pydantic-settings, fhir.resources (R4B), pandas, httpx, PyJWT, SQLite (stdlib), pytest + pytest-asyncio.

## Global Constraints

- The LLM is NEVER called synchronously inside a hook handler. Handlers do cache reads and deterministic work only.
- Every hook handler returns a valid CDS Hooks response even on internal error: `{"cards": []}`. Fail silent, never 500 to Epic (except auth 401).
- Catalog grounding is a hard rule: any proposed order set / item / variant not resolvable in the catalog is dropped and recorded as a violation.
- No PHI in application logs. Patient identifiers go only to the audit store.
- Epic code systems (exact strings): `urn:com.epic.cdshooks.action.code.system.preference-list-item` and `urn:com.epic.cdshooks.action.code.system.orderset-item`.
- One action per CDS suggestion; `selectionBehavior` is always `"any"`.
- Package name: `epicvibe`, src layout. All env vars prefixed `EPICVIBE_`.
- Windows dev machine: use `python -m pytest` (not bare `pytest`), forward slashes fine in git bash.

## File Structure

```
pyproject.toml
.env.example
src/epicvibe/
  config.py                 # Settings (pydantic-settings)
  cache.py                  # ProposalCache (in-memory, TTL)
  jobs.py                   # JobRunner (asyncio background tasks, dedupe)
  catalog/
    models.py               # Catalog, OrderSet, OrderGroup, OrderItem, ItemVariant, CatalogMeta
    loader.py               # load_catalog(path) -> Catalog (tolerant)
    index.py                # CatalogIndex: lookups + shortlist
    tsv_ingest.py           # ingest_tsv(dir) -> Catalog (pandas)
  proposal/
    schema.py               # Proposal models + proposal_json_schema()
    patient_summary.py      # summarize_prefetch(context, prefetch) -> PatientSummary
    validation.py           # validate_proposal(raw, index) -> ValidatedProposal
    engine.py               # ProposalEngine
  inference/
    base.py                 # InferenceProvider protocol, FakeProvider
    anthropic_provider.py   # AnthropicProvider
    factory.py              # make_provider(settings)
  cds/
    auth.py                 # verify_epic_jwt + FastAPI dependency
    cards.py                # render_* card emitters
    hooks.py                # discovery + 3 hook handlers + feedback (APIRouter)
    app.py                  # create_app(settings, *, provider=None)
  audit/
    store.py                # AuditStore (sqlite3)
evals/
  run.py                    # eval harness CLI
  scoring.py                # score(vp, expected, name) -> ScenarioScore
  scenarios/dm2_new_dx.json
fixtures/
  catalog/sample_catalog.json
  extract/order_sets.tsv, order_set_lines.tsv
  hooks/patient_view.json, order_select.json
tests/  (mirrors src: test_catalog_models.py, test_loader.py, ...)
docs/runbook-sandbox.md
```

---

### Task 1: Project scaffold + Settings

**Files:**
- Create: `pyproject.toml`, `.env.example`, `src/epicvibe/__init__.py`, `src/epicvibe/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `epicvibe.config.Settings` with fields exactly: `catalog_path: Path`, `inference_provider: Literal["fake","anthropic"]`, `anthropic_api_key: str`, `anthropic_model: str`, `anthropic_base_url: str`, `verify_jwt: bool`, `jwks_url: str`, `jwt_audience: str`, `audit_db_path: Path`, `cache_ttl_seconds: int`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "epicvibe"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "fhir.resources>=7.1",
    "pandas>=2.2",
    "httpx>=0.27",
    "PyJWT[crypto]>=2.8",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create venv and install**

Run: `python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`
Expected: installs without error.

- [ ] **Step 3: Write the failing test** — `tests/test_config.py`

```python
from pathlib import Path
from epicvibe.config import Settings

def test_defaults():
    s = Settings(_env_file=None)
    assert s.inference_provider == "fake"
    assert s.verify_jwt is False
    assert s.catalog_path == Path("fixtures/catalog/sample_catalog.json")

def test_env_override(monkeypatch):
    monkeypatch.setenv("EPICVIBE_INFERENCE_PROVIDER", "anthropic")
    assert Settings(_env_file=None).inference_provider == "anthropic"
```

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v` — Expected: FAIL (module not found).

- [ ] **Step 4: Implement** — `src/epicvibe/config.py` (and empty `src/epicvibe/__init__.py`)

```python
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EPICVIBE_", env_file=".env")

    catalog_path: Path = Path("fixtures/catalog/sample_catalog.json")
    inference_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_base_url: str = "https://api.anthropic.com"
    verify_jwt: bool = False
    jwks_url: str = ""
    jwt_audience: str = ""
    audit_db_path: Path = Path("audit.db")
    cache_ttl_seconds: int = 3600
```

`.env.example`: the ten variables above as `EPICVIBE_*=` lines with the same defaults, plus a comment that `EPICVIBE_ANTHROPIC_API_KEY` is required when `EPICVIBE_INFERENCE_PROVIDER=anthropic`.

- [ ] **Step 5: Run tests (expect PASS), then commit**

```bash
git add pyproject.toml .env.example src tests
git commit -m "feat: project scaffold and settings"
```

---

### Task 2: Catalog models + sample catalog fixture

**Files:**
- Create: `src/epicvibe/catalog/__init__.py` (empty), `src/epicvibe/catalog/models.py`, `fixtures/catalog/sample_catalog.json`
- Test: `tests/test_catalog_models.py`

**Interfaces:**
- Produces: `epicvibe.catalog.models` — `ItemVariant(variant_id, code_system, code, display, defaults: dict[str,str])`, `OrderItem(item_id, name, order_type: Literal["medication","procedure"], default_selected: bool, variants: list[ItemVariant])`, `OrderGroup(group_id, name, items)`, `OrderSet(order_set_id, name, keywords: list[str], icd10_codes: list[str], groups)`, `CatalogMeta(epic_version, extracted_at, site_id)`, `Catalog(meta, order_sets)`. All models use `ConfigDict(extra="allow")`.

- [ ] **Step 1: Write the failing test** — `tests/test_catalog_models.py`

```python
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
```

Run: `.venv/Scripts/python -m pytest tests/test_catalog_models.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement models** — `src/epicvibe/catalog/models.py`

```python
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
```

- [ ] **Step 3: Create `fixtures/catalog/sample_catalog.json`**

```json
{
  "meta": {"epic_version": "Feb 2026", "extracted_at": "2026-08-01T00:00:00Z", "site_id": "CUSTOMER1"},
  "order_sets": [
    {
      "order_set_id": "AMB_DM2_NEWDX",
      "name": "Diabetes Mellitus Type 2 - New Diagnosis (Ambulatory)",
      "keywords": ["diabetes", "a1c", "metformin", "glucose"],
      "icd10_codes": ["E11"],
      "groups": [
        {"group_id": "G_LABS", "name": "Laboratory", "items": [
          {"item_id": "ITEM_A1C", "name": "Hemoglobin A1c", "order_type": "procedure", "default_selected": true,
           "variants": [{"variant_id": "V_A1C", "code": "PREF_A1C", "display": "Hemoglobin A1c", "defaults": {"priority": "Routine"}}]},
          {"item_id": "ITEM_LIPID", "name": "Lipid Panel", "order_type": "procedure", "default_selected": true,
           "variants": [{"variant_id": "V_LIPID", "code": "PREF_LIPID", "display": "Lipid Panel", "defaults": {"priority": "Routine"}}]},
          {"item_id": "ITEM_UMALB", "name": "Urine Microalbumin/Creatinine Ratio", "order_type": "procedure", "default_selected": true,
           "variants": [{"variant_id": "V_UMALB", "code": "PREF_UMALB", "display": "Urine Microalbumin/Creatinine Ratio", "defaults": {"priority": "Routine"}}]}
        ]},
        {"group_id": "G_MEDS", "name": "Medications", "items": [
          {"item_id": "ITEM_METFORMIN", "name": "Metformin", "order_type": "medication", "default_selected": true,
           "variants": [
             {"variant_id": "V_MET_500", "code": "PREF_MET_500", "display": "metFORMIN 500 mg tablet BID", "defaults": {"dose": "500 mg", "route": "oral", "frequency": "BID"}},
             {"variant_id": "V_MET_1000", "code": "PREF_MET_1000", "display": "metFORMIN 1000 mg tablet BID", "defaults": {"dose": "1000 mg", "route": "oral", "frequency": "BID"}}
           ]}
        ]},
        {"group_id": "G_REF", "name": "Referrals", "items": [
          {"item_id": "ITEM_RETINAL", "name": "Ophthalmology Referral - Diabetic Retinal Exam", "order_type": "procedure", "default_selected": false,
           "variants": [{"variant_id": "V_RETINAL", "code": "PREF_RETINAL", "display": "Referral to Ophthalmology", "defaults": {}}]}
        ]}
      ]
    },
    {
      "order_set_id": "AMB_HTN",
      "name": "Hypertension Management (Ambulatory)",
      "keywords": ["hypertension", "blood pressure"],
      "icd10_codes": ["I10"],
      "groups": [
        {"group_id": "G_LABS", "name": "Laboratory", "items": [
          {"item_id": "ITEM_BMP", "name": "Basic Metabolic Panel", "order_type": "procedure", "default_selected": true,
           "variants": [{"variant_id": "V_BMP", "code": "PREF_BMP", "display": "Basic Metabolic Panel", "defaults": {"priority": "Routine"}}]}
        ]}
      ]
    }
  ]
}
```

- [ ] **Step 4: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/catalog fixtures/catalog tests/test_catalog_models.py
git commit -m "feat: catalog models and sample catalog fixture"
```

---

### Task 3: Catalog JSON loader (tolerant)

**Files:**
- Create: `src/epicvibe/catalog/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `Catalog` from Task 2.
- Produces: `epicvibe.catalog.loader.load_catalog(path: Path) -> Catalog`. Logs a WARNING (logger name `epicvibe.catalog`) listing unknown top-level keys per order set; raises `FileNotFoundError` if missing, `ValueError` on invalid JSON/schema.

- [ ] **Step 1: Write the failing test** — `tests/test_loader.py`

```python
import json
import logging
from pathlib import Path
import pytest
from epicvibe.catalog.loader import load_catalog

def test_loads_sample():
    cat = load_catalog(Path("fixtures/catalog/sample_catalog.json"))
    assert len(cat.order_sets) == 2

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
```

Run: `.venv/Scripts/python -m pytest tests/test_loader.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/catalog/loader.py`

```python
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
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/catalog/loader.py tests/test_loader.py
git commit -m "feat: tolerant catalog JSON loader"
```

---

### Task 4: CatalogIndex (lookups + shortlist)

**Files:**
- Create: `src/epicvibe/catalog/index.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Consumes: Task 2 models, Task 3 loader.
- Produces: `epicvibe.catalog.index.CatalogIndex(catalog: Catalog)` with:
  - `get_order_set(order_set_id: str) -> OrderSet | None`
  - `get_item(item_id: str) -> tuple[OrderSet, OrderItem] | None`
  - `get_variant(item_id: str, variant_id: str) -> ItemVariant | None`
  - `shortlist(icd10_codes: set[str], keywords: set[str]) -> list[OrderSet]` — an order set matches if any patient ICD-10 code starts with any of its `icd10_codes` prefixes (case-insensitive), or any keyword appears in its name/keywords (case-insensitive).

- [ ] **Step 1: Write the failing test** — `tests/test_index.py`

```python
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
```

Run: `.venv/Scripts/python -m pytest tests/test_index.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/catalog/index.py`

```python
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
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/catalog/index.py tests/test_index.py
git commit -m "feat: catalog index with lookups and shortlist"
```

---

### Task 5: TSV extract ingest (pandas)

**Files:**
- Create: `src/epicvibe/catalog/tsv_ingest.py`, `fixtures/extract/order_sets.tsv`, `fixtures/extract/order_set_lines.tsv`
- Test: `tests/test_tsv_ingest.py`

**Interfaces:**
- Consumes: Task 2 models.
- Produces: `epicvibe.catalog.tsv_ingest.ingest_tsv(extract_dir: Path) -> Catalog`. Unknown columns → WARNING on logger `epicvibe.catalog`, never fail. Files other than the two known TSVs are ignored.
- NOTE: the fixture mimics a Clarity-flavored flat export. **When the customer's real extract files land, this module (and only this module) is adapted; column mapping lives in the `_COLMAP` dicts at the top.**

- [ ] **Step 1: Create fixtures**

`fixtures/extract/order_sets.tsv` (tab-separated):

```
ORDER_SET_ID	NAME	KEYWORDS	ICD10_CODES	SITE_CUSTOM_COL
AMB_DM2_NEWDX	Diabetes Mellitus Type 2 - New Diagnosis (Ambulatory)	diabetes;a1c;metformin	E11	x
```

`fixtures/extract/order_set_lines.tsv`:

```
ORDER_SET_ID	GROUP_ID	GROUP_NAME	ITEM_ID	ITEM_NAME	ORDER_TYPE	DEFAULT_SELECTED	VARIANT_ID	CODE	DISPLAY	DOSE	ROUTE	FREQUENCY
AMB_DM2_NEWDX	G_LABS	Laboratory	ITEM_A1C	Hemoglobin A1c	procedure	Y	V_A1C	PREF_A1C	Hemoglobin A1c			
AMB_DM2_NEWDX	G_MEDS	Medications	ITEM_METFORMIN	Metformin	medication	Y	V_MET_500	PREF_MET_500	metFORMIN 500 mg tablet BID	500 mg	oral	BID
AMB_DM2_NEWDX	G_MEDS	Medications	ITEM_METFORMIN	Metformin	medication	Y	V_MET_1000	PREF_MET_1000	metFORMIN 1000 mg tablet BID	1000 mg	oral	BID
```

- [ ] **Step 2: Write the failing test** — `tests/test_tsv_ingest.py`

```python
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
```

Run: `.venv/Scripts/python -m pytest tests/test_tsv_ingest.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** — `src/epicvibe/catalog/tsv_ingest.py`

```python
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
```

- [ ] **Step 4: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/catalog/tsv_ingest.py fixtures/extract tests/test_tsv_ingest.py
git commit -m "feat: pandas TSV extract ingest with tolerance rules"
```

---

### Task 6: Proposal schema + LLM JSON schema export

**Files:**
- Create: `src/epicvibe/proposal/__init__.py` (empty), `src/epicvibe/proposal/schema.py`
- Test: `tests/test_proposal_schema.py`

**Interfaces:**
- Produces: `epicvibe.proposal.schema` — `ParameterRecommendation(label, value, rationale)`, `ProposedItem(item_id, variant_id, include: bool, rationale, parameter_recommendations: list[ParameterRecommendation] = [])`, `OrderSetProposal(order_set_id, rationale, items: list[ProposedItem])`, `Proposal(order_sets: list[OrderSetProposal], confidence: Literal["high","medium","low"])`, `proposal_json_schema() -> dict`.

- [ ] **Step 1: Write the failing test** — `tests/test_proposal_schema.py`

```python
import pytest
from pydantic import ValidationError
from epicvibe.proposal.schema import Proposal, proposal_json_schema

def test_roundtrip():
    raw = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx",
            "items": [{"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True,
                       "rationale": "no A1c in 9 months"}]}], "confidence": "high"}
    p = Proposal.model_validate(raw)
    assert p.order_sets[0].items[0].parameter_recommendations == []

def test_rejects_bad_confidence():
    with pytest.raises(ValidationError):
        Proposal.model_validate({"order_sets": [], "confidence": "certain"})

def test_json_schema_exports():
    schema = proposal_json_schema()
    assert schema["type"] == "object" and "order_sets" in schema["properties"]
```

Run: `.venv/Scripts/python -m pytest tests/test_proposal_schema.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/proposal/schema.py`

```python
from typing import Literal

from pydantic import BaseModel


class ParameterRecommendation(BaseModel):
    label: str
    value: str
    rationale: str


class ProposedItem(BaseModel):
    item_id: str
    variant_id: str
    include: bool
    rationale: str
    parameter_recommendations: list[ParameterRecommendation] = []


class OrderSetProposal(BaseModel):
    order_set_id: str
    rationale: str
    items: list[ProposedItem]


class Proposal(BaseModel):
    order_sets: list[OrderSetProposal]
    confidence: Literal["high", "medium", "low"]


def proposal_json_schema() -> dict:
    return Proposal.model_json_schema()
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/proposal tests/test_proposal_schema.py
git commit -m "feat: proposal schema shared with LLM structured output"
```

---

### Task 7: Patient summary from hook prefetch

**Files:**
- Create: `src/epicvibe/proposal/patient_summary.py`, `fixtures/hooks/patient_view.json`
- Test: `tests/test_patient_summary.py`

**Interfaces:**
- Consumes: nothing internal (parses FHIR with `fhir.resources.R4B`).
- Produces: `epicvibe.proposal.patient_summary` — `CodedItem(code, system, display)`, `PatientSummary(patient_id, encounter_id: str | None, conditions: list[CodedItem], medications: list[CodedItem], notes: list[str])`, and `summarize_prefetch(context: dict, prefetch: dict) -> PatientSummary`. Missing/malformed prefetch entries are skipped silently (fail-soft per spec §7); `notes` carries data-quality remarks like `"prefetch key 'medications' missing"`.

- [ ] **Step 1: Create fixture** — `fixtures/hooks/patient_view.json`

```json
{
  "hook": "patient-view",
  "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
  "fhirServer": "https://example.org/api/FHIR/R4",
  "fhirAuthorization": {"access_token": "sandbox-token", "token_type": "Bearer",
    "expires_in": 300, "scope": "patient/Patient.read", "subject": "epicvibe"},
  "context": {"userId": "Practitioner/PRACT1", "patientId": "PAT1", "encounterId": "ENC1"},
  "prefetch": {
    "conditions": {"resourceType": "Bundle", "type": "searchset", "entry": [
      {"resource": {"resourceType": "Condition", "id": "c1",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9",
                             "display": "Type 2 diabetes mellitus without complications"}]},
        "subject": {"reference": "Patient/PAT1"}}}]},
    "medications": {"resourceType": "Bundle", "type": "searchset", "entry": [
      {"resource": {"resourceType": "MedicationRequest", "id": "m1", "status": "active", "intent": "order",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                                  "code": "310965", "display": "lisinopril 10 MG Oral Tablet"}]},
        "subject": {"reference": "Patient/PAT1"}}}]}
  }
}
```

- [ ] **Step 2: Write the failing test** — `tests/test_patient_summary.py`

```python
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
```

Run: `.venv/Scripts/python -m pytest tests/test_patient_summary.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** — `src/epicvibe/proposal/patient_summary.py`

```python
import logging

from fhir.resources.R4B.bundle import Bundle
from pydantic import BaseModel

log = logging.getLogger("epicvibe.proposal")


class CodedItem(BaseModel):
    code: str
    system: str = ""
    display: str = ""


class PatientSummary(BaseModel):
    patient_id: str
    encounter_id: str | None = None
    conditions: list[CodedItem] = []
    medications: list[CodedItem] = []
    notes: list[str] = []


def _codings(resource, attr: str) -> list[CodedItem]:
    concept = getattr(resource, attr, None)
    if concept is None or not concept.coding:
        return []
    return [CodedItem(code=c.code or "", system=c.system or "", display=c.display or "")
            for c in concept.coding if c.code]


def _parse_bundle(prefetch: dict, key: str, notes: list[str]) -> list:
    raw = prefetch.get(key)
    if raw is None:
        notes.append(f"prefetch key '{key}' missing")
        return []
    try:
        bundle = Bundle.model_validate(raw)
    except Exception:
        notes.append(f"prefetch key '{key}' malformed")
        return []
    return [e.resource for e in (bundle.entry or []) if e.resource is not None]


def summarize_prefetch(context: dict, prefetch: dict) -> PatientSummary:
    notes: list[str] = []
    conditions, medications = [], []
    for res in _parse_bundle(prefetch, "conditions", notes):
        conditions.extend(_codings(res, "code"))
    for res in _parse_bundle(prefetch, "medications", notes):
        medications.extend(_codings(res, "medicationCodeableConcept"))
    return PatientSummary(patient_id=context["patientId"],
                          encounter_id=context.get("encounterId"),
                          conditions=conditions, medications=medications, notes=notes)
```

- [ ] **Step 4: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/proposal/patient_summary.py fixtures/hooks/patient_view.json tests/test_patient_summary.py
git commit -m "feat: patient summary from hook prefetch"
```

---

### Task 8: Inference providers (protocol, fake, Anthropic)

**Files:**
- Create: `src/epicvibe/inference/__init__.py` (empty), `src/epicvibe/inference/base.py`, `src/epicvibe/inference/anthropic_provider.py`, `src/epicvibe/inference/factory.py`
- Test: `tests/test_inference.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces:
  - `epicvibe.inference.base.InferenceProvider` (Protocol): `async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict`
  - `epicvibe.inference.base.FakeProvider(response: dict)` — returns `response` verbatim.
  - `epicvibe.inference.anthropic_provider.AnthropicProvider(api_key: str, model: str, base_url: str = "https://api.anthropic.com", client: httpx.AsyncClient | None = None)` — forced tool-use structured output.
  - `epicvibe.inference.factory.make_provider(settings: Settings) -> InferenceProvider` — `"fake"` → `FakeProvider({"order_sets": [], "confidence": "low"})`; `"anthropic"` → `AnthropicProvider` from settings.

- [ ] **Step 1: Write the failing test** — `tests/test_inference.py`

```python
import httpx
import pytest
from epicvibe.inference.anthropic_provider import AnthropicProvider
from epicvibe.inference.base import FakeProvider

async def test_fake():
    p = FakeProvider({"order_sets": [], "confidence": "low"})
    out = await p.complete_json(system="s", user="u", json_schema={})
    assert out == {"order_sets": [], "confidence": "low"}

async def test_anthropic_parses_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "k"
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "name": "emit_proposal",
             "input": {"order_sets": [], "confidence": "high"}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.anthropic.com")
    p = AnthropicProvider(api_key="k", model="m", client=client)
    out = await p.complete_json(system="s", user="u", json_schema={"type": "object"})
    assert out["confidence"] == "high"

async def test_anthropic_raises_without_tool_use():
    def handler(request):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.anthropic.com")
    p = AnthropicProvider(api_key="k", model="m", client=client)
    with pytest.raises(ValueError):
        await p.complete_json(system="s", user="u", json_schema={})
```

Run: `.venv/Scripts/python -m pytest tests/test_inference.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/inference/base.py`

```python
from typing import Protocol


class InferenceProvider(Protocol):
    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict: ...


class FakeProvider:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user})
        return self.response
```

`src/epicvibe/inference/anthropic_provider.py`:

```python
import httpx


class AnthropicProvider:
    def __init__(self, api_key: str, model: str,
                 base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None):
        self.model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=30.0,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        if client is not None:
            self._client.headers.setdefault("x-api-key", api_key)
            self._client.headers.setdefault("anthropic-version", "2023-06-01")

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        resp = await self._client.post("/v1/messages", json={
            "model": self.model, "max_tokens": 2048, "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "emit_proposal",
                       "description": "Emit the order proposal.",
                       "input_schema": json_schema}],
            "tool_choice": {"type": "tool", "name": "emit_proposal"},
        })
        resp.raise_for_status()
        for block in resp.json().get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        raise ValueError("no tool_use block in model response")
```

`src/epicvibe/inference/factory.py`:

```python
from epicvibe.config import Settings
from epicvibe.inference.anthropic_provider import AnthropicProvider
from epicvibe.inference.base import FakeProvider, InferenceProvider


def make_provider(settings: Settings) -> InferenceProvider:
    if settings.inference_provider == "anthropic":
        return AnthropicProvider(api_key=settings.anthropic_api_key,
                                 model=settings.anthropic_model,
                                 base_url=settings.anthropic_base_url)
    return FakeProvider({"order_sets": [], "confidence": "low"})
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/inference tests/test_inference.py
git commit -m "feat: pluggable inference providers (fake + anthropic)"
```

---

### Task 9: Grounding validation

**Files:**
- Create: `src/epicvibe/proposal/validation.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: `Proposal` (Task 6), `CatalogIndex` (Task 4).
- Produces: `epicvibe.proposal.validation` — `GroundingViolation(kind: Literal["order_set","item","variant","membership","schema"], detail: str)`, `ValidatedProposal(proposal: Proposal, violations: list[GroundingViolation])` with property `is_empty: bool` (no order sets with included items), and `validate_proposal(raw: dict, index: CatalogIndex) -> ValidatedProposal`. Rules: schema-invalid raw → empty proposal + one `schema` violation; unknown `order_set_id` → drop set; unknown `item_id`/`variant_id` or item not belonging to that order set → drop item. Each drop is one violation. Confidence preserved; empty/invalid → `"low"`.

- [ ] **Step 1: Write the failing test** — `tests/test_validation.py`

```python
from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.proposal.validation import validate_proposal

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

def _item(item_id="ITEM_A1C", variant_id="V_A1C"):
    return {"item_id": item_id, "variant_id": variant_id, "include": True, "rationale": "r"}

def _raw(items):
    return {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "r", "items": items}],
            "confidence": "high"}

def test_valid_passes_clean():
    vp = validate_proposal(_raw([_item()]), IDX)
    assert vp.violations == [] and not vp.is_empty

def test_unknown_item_dropped():
    vp = validate_proposal(_raw([_item(), _item(item_id="ITEM_FAKE")]), IDX)
    assert len(vp.violations) == 1 and vp.violations[0].kind == "item"
    assert len(vp.proposal.order_sets[0].items) == 1

def test_unknown_variant_dropped():
    vp = validate_proposal(_raw([_item(variant_id="V_FAKE")]), IDX)
    assert vp.violations[0].kind == "variant" and vp.is_empty

def test_wrong_set_membership_dropped():
    vp = validate_proposal(_raw([_item(item_id="ITEM_BMP", variant_id="V_BMP")]), IDX)
    assert vp.violations[0].kind == "membership"

def test_unknown_order_set_dropped():
    raw = {"order_sets": [{"order_set_id": "NOPE", "rationale": "r", "items": [_item()]}],
           "confidence": "high"}
    vp = validate_proposal(raw, IDX)
    assert vp.violations[0].kind == "order_set" and vp.is_empty

def test_schema_garbage():
    vp = validate_proposal({"nope": 1}, IDX)
    assert vp.violations[0].kind == "schema" and vp.is_empty
    assert vp.proposal.confidence == "low"
```

Run: `.venv/Scripts/python -m pytest tests/test_validation.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/proposal/validation.py`

```python
import logging
from typing import Literal

from pydantic import BaseModel, ValidationError

from epicvibe.catalog.index import CatalogIndex
from epicvibe.proposal.schema import OrderSetProposal, Proposal

log = logging.getLogger("epicvibe.proposal")


class GroundingViolation(BaseModel):
    kind: Literal["order_set", "item", "variant", "membership", "schema"]
    detail: str


class ValidatedProposal(BaseModel):
    proposal: Proposal
    violations: list[GroundingViolation] = []

    @property
    def is_empty(self) -> bool:
        return not any(i.include for o in self.proposal.order_sets for i in o.items)


def _empty() -> Proposal:
    return Proposal(order_sets=[], confidence="low")


def validate_proposal(raw: dict, index: CatalogIndex) -> ValidatedProposal:
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError as e:
        log.warning("proposal failed schema validation: %s", e.error_count())
        return ValidatedProposal(proposal=_empty(),
                                 violations=[GroundingViolation(kind="schema", detail=str(e.error_count()) + " schema errors")])

    violations: list[GroundingViolation] = []
    kept_sets: list[OrderSetProposal] = []
    for osp in proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        if oset is None:
            violations.append(GroundingViolation(kind="order_set", detail=osp.order_set_id))
            continue
        kept_items = []
        for pi in osp.items:
            found = index.get_item(pi.item_id)
            if found is None:
                violations.append(GroundingViolation(kind="item", detail=pi.item_id))
                continue
            owner, _ = found
            if owner.order_set_id != osp.order_set_id:
                violations.append(GroundingViolation(
                    kind="membership", detail=f"{pi.item_id} not in {osp.order_set_id}"))
                continue
            if index.get_variant(pi.item_id, pi.variant_id) is None:
                violations.append(GroundingViolation(
                    kind="variant", detail=f"{pi.item_id}/{pi.variant_id}"))
                continue
            kept_items.append(pi)
        kept_sets.append(OrderSetProposal(order_set_id=osp.order_set_id,
                                          rationale=osp.rationale, items=kept_items))
    for v in violations:
        log.warning("grounding violation [%s]: %s", v.kind, v.detail)
    return ValidatedProposal(
        proposal=Proposal(order_sets=kept_sets, confidence=proposal.confidence),
        violations=violations)
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/proposal/validation.py tests/test_validation.py
git commit -m "feat: catalog grounding validation with violation tracking"
```

---

### Task 10: Proposal engine

**Files:**
- Create: `src/epicvibe/proposal/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `CatalogIndex.shortlist` (Task 4), `PatientSummary` (Task 7), `InferenceProvider` (Task 8), `validate_proposal` (Task 9), `proposal_json_schema` (Task 6).
- Produces: `epicvibe.proposal.engine.ProposalEngine(index: CatalogIndex, provider: InferenceProvider)` with `async def generate(self, summary: PatientSummary) -> ValidatedProposal`. Empty shortlist → empty ValidatedProposal without calling the provider. Provider exception → empty ValidatedProposal (fail-soft), logged.

- [ ] **Step 1: Write the failing test** — `tests/test_engine.py`

```python
from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.inference.base import FakeProvider
from epicvibe.proposal.engine import ProposalEngine
from epicvibe.proposal.patient_summary import CodedItem, PatientSummary

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

def _summary():
    return PatientSummary(patient_id="PAT1", encounter_id="ENC1",
        conditions=[CodedItem(code="E11.9", system="icd10", display="Type 2 diabetes")])

GOOD = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx",
        "items": [{"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "r"}]}],
        "confidence": "high"}

async def test_generates_validated_proposal():
    provider = FakeProvider(GOOD)
    vp = await ProposalEngine(IDX, provider).generate(_summary())
    assert not vp.is_empty and vp.violations == []
    assert "AMB_DM2_NEWDX" in provider.calls[0]["user"]      # shortlist in prompt
    assert "AMB_HTN" not in provider.calls[0]["user"]        # non-matching set excluded

async def test_no_shortlist_skips_llm():
    provider = FakeProvider(GOOD)
    summary = PatientSummary(patient_id="PAT1", conditions=[CodedItem(code="Z00.0")])
    vp = await ProposalEngine(IDX, provider).generate(summary)
    assert vp.is_empty and provider.calls == []

async def test_provider_error_is_soft():
    class Boom:
        async def complete_json(self, **kw):
            raise RuntimeError("api down")
    vp = await ProposalEngine(IDX, Boom()).generate(_summary())
    assert vp.is_empty
```

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/proposal/engine.py`

```python
import json
import logging

from epicvibe.catalog.index import CatalogIndex
from epicvibe.inference.base import InferenceProvider
from epicvibe.proposal.patient_summary import PatientSummary
from epicvibe.proposal.schema import Proposal, proposal_json_schema
from epicvibe.proposal.validation import ValidatedProposal, validate_proposal

log = logging.getLogger("epicvibe.proposal")

SYSTEM_PROMPT = """You are an order-set selection assistant for ambulatory clinicians.
Given a patient summary and this institution's approved order-set catalog slices,
select the applicable order set(s), which component items to include or exclude,
and the most clinically appropriate pre-built variant of each item.
Rules:
- Propose ONLY order sets, items, and variants present in the provided catalog.
- Give a short clinical rationale per item.
- Parameter recommendations are advisory display text only; they are never applied.
- If nothing applies, return an empty order_sets list with confidence "low"."""


def _empty() -> ValidatedProposal:
    return ValidatedProposal(proposal=Proposal(order_sets=[], confidence="low"))


class ProposalEngine:
    def __init__(self, index: CatalogIndex, provider: InferenceProvider):
        self.index = index
        self.provider = provider

    async def generate(self, summary: PatientSummary) -> ValidatedProposal:
        codes = {c.code for c in summary.conditions if c.code}
        keywords = {w for c in summary.conditions for w in c.display.lower().split()}
        shortlist = self.index.shortlist(codes, keywords)
        if not shortlist:
            return _empty()
        user = json.dumps({
            "patient": summary.model_dump(exclude={"patient_id", "encounter_id"}),
            "catalog": [o.model_dump() for o in shortlist],
        })
        try:
            raw = await self.provider.complete_json(
                system=SYSTEM_PROMPT, user=user, json_schema=proposal_json_schema())
        except Exception:
            log.exception("inference call failed")
            return _empty()
        return validate_proposal(raw, self.index)
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/proposal/engine.py tests/test_engine.py
git commit -m "feat: proposal engine with shortlist retrieval and fail-soft inference"
```

---

### Task 11: Encounter cache + background job runner

**Files:**
- Create: `src/epicvibe/cache.py`, `src/epicvibe/jobs.py`
- Test: `tests/test_cache_jobs.py`

**Interfaces:**
- Consumes: `ValidatedProposal` (Task 9) as the cached value type (cache is generic in practice).
- Produces:
  - `epicvibe.cache.ProposalCache(ttl_seconds: int, clock: Callable[[], float] = time.monotonic)` with `get(key: str)`, `put(key: str, value)`.
  - `epicvibe.jobs.JobRunner()` with `enqueue(key: str, coro_factory: Callable[[], Coroutine]) -> bool` (False if a job for `key` is already running; exceptions inside jobs are logged, never propagated) and `async def join(self)` (await all pending jobs; for tests/evals).

- [ ] **Step 1: Write the failing test** — `tests/test_cache_jobs.py`

```python
import asyncio
from epicvibe.cache import ProposalCache
from epicvibe.jobs import JobRunner

def test_cache_ttl():
    now = [0.0]
    c = ProposalCache(ttl_seconds=10, clock=lambda: now[0])
    c.put("ENC1", "value")
    assert c.get("ENC1") == "value"
    now[0] = 11.0
    assert c.get("ENC1") is None
    assert c.get("MISSING") is None

async def test_runner_dedupes_and_joins():
    runner = JobRunner()
    done = []
    async def work():
        await asyncio.sleep(0)
        done.append(1)
    assert runner.enqueue("k", work) is True
    assert runner.enqueue("k", work) is False     # deduped while running
    await runner.join()
    assert done == [1]
    assert runner.enqueue("k", work) is True      # can run again after completion

async def test_runner_swallows_exceptions():
    runner = JobRunner()
    async def boom():
        raise RuntimeError("x")
    runner.enqueue("k", boom)
    await runner.join()                           # must not raise
```

Run: `.venv/Scripts/python -m pytest tests/test_cache_jobs.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/cache.py`

```python
import time
from typing import Any, Callable


class ProposalCache:
    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if self._clock() - stored_at > self._ttl:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (self._clock(), value)
```

`src/epicvibe/jobs.py`:

```python
import asyncio
import logging
from typing import Callable, Coroutine

log = logging.getLogger("epicvibe.jobs")


class JobRunner:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, key: str, coro_factory: Callable[[], Coroutine]) -> bool:
        if key in self._tasks and not self._tasks[key].done():
            return False
        task = asyncio.create_task(self._run(key, coro_factory))
        self._tasks[key] = task
        return True

    async def _run(self, key: str, coro_factory):
        try:
            await coro_factory()
        except Exception:
            log.exception("background job failed (key=%s)", key)
        finally:
            self._tasks.pop(key, None)

    async def join(self) -> None:
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/cache.py src/epicvibe/jobs.py tests/test_cache_jobs.py
git commit -m "feat: encounter proposal cache and dedup job runner"
```

---

### Task 12: Card emitters

**Files:**
- Create: `src/epicvibe/cds/__init__.py` (empty), `src/epicvibe/cds/cards.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: `ValidatedProposal` (Task 9), `CatalogIndex` (Task 4).
- Produces: `epicvibe.cds.cards` —
  - `render_suggestion_cards(vp: ValidatedProposal, index: CatalogIndex, patient_id: str, exclude_codes: frozenset[str] = frozenset()) -> list[dict]` — one card per proposed order set; one suggestion per included item (uuid, label, `isRecommended: True`, single `create` action with a draft MedicationRequest/ServiceRequest stub); card has `summary` (≤140 chars), markdown `detail` including parameter recommendations, `indicator: "info"`, `source: {"label": "EpicVibe Ordering"}`, `selectionBehavior: "any"`.
  - `render_summary_card(vp) -> dict` — informational card, no suggestions.
  - `render_missing_items_card(vp, index, draft_codes: frozenset[str]) -> dict | None` — informational card listing included items whose variant code is not in `draft_codes`; None if none missing.

- [ ] **Step 1: Write the failing test** — `tests/test_cards.py`

```python
from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                render_summary_card)
from epicvibe.proposal.validation import validate_proposal

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))

RAW = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "New T2DM diagnosis",
    "items": [
        {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "baseline"},
        {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True,
         "rationale": "first-line",
         "parameter_recommendations": [{"label": "dose", "value": "500 mg BID", "rationale": "start low"}]},
        {"item_id": "ITEM_RETINAL", "variant_id": "V_RETINAL", "include": False, "rationale": "recent exam"}]}],
    "confidence": "high"}
VP = validate_proposal(RAW, IDX)

def test_suggestion_cards():
    cards = render_suggestion_cards(VP, IDX, "PAT1")
    assert len(cards) == 1
    card = cards[0]
    assert card["selectionBehavior"] == "any" and card["indicator"] == "info"
    assert len(card["suggestions"]) == 2                       # excluded item omitted
    med = [s for s in card["suggestions"] if "metFORMIN" in s["label"]][0]
    assert len(med["actions"]) == 1                            # one action per suggestion
    res = med["actions"][0]["resource"]
    assert res["resourceType"] == "MedicationRequest" and res["status"] == "draft"
    assert res["medicationCodeableConcept"]["coding"][0]["code"] == "PREF_MET_500"
    assert res["subject"]["reference"] == "Patient/PAT1"
    a1c = [s for s in card["suggestions"] if s["label"] == "Hemoglobin A1c"][0]
    assert a1c["actions"][0]["resource"]["resourceType"] == "ServiceRequest"
    assert "500 mg BID" in card["detail"]                      # advisory values in markdown

def test_exclude_codes():
    cards = render_suggestion_cards(VP, IDX, "PAT1", exclude_codes=frozenset({"PREF_A1C"}))
    assert len(cards[0]["suggestions"]) == 1

def test_summary_card():
    card = render_summary_card(VP)
    assert "suggestions" not in card and "2" in card["summary"]

def test_missing_items_card():
    card = render_missing_items_card(VP, IDX, frozenset({"PREF_MET_500"}))
    assert "Hemoglobin A1c" in card["detail"]
    assert render_missing_items_card(VP, IDX, frozenset({"PREF_MET_500", "PREF_A1C"})) is None
```

Run: `.venv/Scripts/python -m pytest tests/test_cards.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/cds/cards.py`

```python
from uuid import uuid4

from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.models import ItemVariant, OrderItem
from epicvibe.proposal.validation import ValidatedProposal

SOURCE = {"label": "EpicVibe Ordering"}


def _resource_stub(item: OrderItem, variant: ItemVariant, patient_id: str) -> dict:
    coding = {"system": variant.code_system, "code": variant.code, "display": variant.display}
    subject = {"reference": f"Patient/{patient_id}"}
    if item.order_type == "medication":
        return {"resourceType": "MedicationRequest", "status": "draft", "intent": "proposal",
                "medicationCodeableConcept": {"coding": [coding]}, "subject": subject}
    return {"resourceType": "ServiceRequest", "status": "draft", "intent": "proposal",
            "code": {"coding": [coding]}, "subject": subject}


def _included(vp: ValidatedProposal):
    for osp in vp.proposal.order_sets:
        for pi in osp.items:
            if pi.include:
                yield osp, pi


def render_suggestion_cards(vp: ValidatedProposal, index: CatalogIndex, patient_id: str,
                            exclude_codes: frozenset[str] = frozenset()) -> list[dict]:
    cards = []
    for osp in vp.proposal.order_sets:
        oset = index.get_order_set(osp.order_set_id)
        suggestions, detail = [], [osp.rationale, ""]
        for pi in osp.items:
            if not pi.include:
                continue
            _, item = index.get_item(pi.item_id)
            variant = index.get_variant(pi.item_id, pi.variant_id)
            if variant.code in exclude_codes:
                continue
            suggestions.append({
                "uuid": str(uuid4()), "label": variant.display, "isRecommended": True,
                "actions": [{"type": "create", "description": pi.rationale,
                             "resource": _resource_stub(item, variant, patient_id)}]})
            recs = "; ".join(f"{r.label}: {r.value}" for r in pi.parameter_recommendations)
            line = f"- **{variant.display}** — {pi.rationale}"
            detail.append(line + (f" _(suggested: {recs})_" if recs else ""))
        if suggestions:
            cards.append({
                "summary": f"{oset.name}: {len(suggestions)} suggested orders"[:140],
                "detail": "\n".join(detail), "indicator": "info", "source": SOURCE,
                "selectionBehavior": "any", "suggestions": suggestions})
    return cards


def render_summary_card(vp: ValidatedProposal) -> dict:
    n = sum(1 for _ in _included(vp))
    names = ", ".join(o.order_set_id for o in vp.proposal.order_sets) or "none"
    return {"summary": f"AI order review ready: {n} suggested orders"[:140],
            "detail": f"Matched order sets: {names}. Suggestions will appear when ordering.",
            "indicator": "info", "source": SOURCE}


def render_missing_items_card(vp: ValidatedProposal, index: CatalogIndex,
                              draft_codes: frozenset[str]) -> dict | None:
    missing = []
    for _, pi in _included(vp):
        variant = index.get_variant(pi.item_id, pi.variant_id)
        if variant.code not in draft_codes:
            missing.append(f"- {variant.display}")
    if not missing:
        return None
    return {"summary": "Protocol items not yet ordered"[:140],
            "detail": "\n".join(missing), "indicator": "info", "source": SOURCE}
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/cds tests/test_cards.py
git commit -m "feat: CDS card emitters (suggestions, summary, missing items)"
```

---

### Task 13: Audit store

**Files:**
- Create: `src/epicvibe/audit/__init__.py` (empty), `src/epicvibe/audit/store.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `ValidatedProposal` (Task 9).
- Produces: `epicvibe.audit.store.AuditStore(path: Path | str)` (`":memory:"` allowed) with:
  - `record_proposal(encounter_key: str, vp: ValidatedProposal, model: str) -> int` (rowid)
  - `record_feedback(service_id: str, payload: dict) -> int` (rows inserted; payload is the CDS Hooks feedback POST body: `{"feedback": [{"card": uuid, "outcome": "accepted"|"overridden", ...}]}`)
  - `proposals() -> list[dict]`, `feedback() -> list[dict]`

- [ ] **Step 1: Write the failing test** — `tests/test_audit.py`

```python
from epicvibe.audit.store import AuditStore
from epicvibe.proposal.schema import Proposal
from epicvibe.proposal.validation import ValidatedProposal

def test_roundtrip():
    store = AuditStore(":memory:")
    vp = ValidatedProposal(proposal=Proposal(order_sets=[], confidence="low"))
    store.record_proposal("ENC1", vp, model="claude-haiku-4-5-20251001")
    rows = store.proposals()
    assert rows[0]["encounter_key"] == "ENC1" and "low" in rows[0]["proposal_json"]

    n = store.record_feedback("epicvibe-order-select",
        {"feedback": [{"card": "uuid-1", "outcome": "accepted", "outcomeTimestamp": "2026-08-04T10:00:00Z"}]})
    assert n == 1
    assert store.feedback()[0]["outcome"] == "accepted"
```

Run: `.venv/Scripts/python -m pytest tests/test_audit.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/audit/store.py`

```python
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from epicvibe.proposal.validation import ValidatedProposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, encounter_key TEXT NOT NULL, created_at TEXT NOT NULL,
  model TEXT NOT NULL, proposal_json TEXT NOT NULL, violations_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, service_id TEXT NOT NULL, card_uuid TEXT,
  outcome TEXT, received_at TEXT NOT NULL, raw_json TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditStore:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def record_proposal(self, encounter_key: str, vp: ValidatedProposal, model: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO proposals (encounter_key, created_at, model, proposal_json, violations_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (encounter_key, _now(), model, vp.proposal.model_dump_json(),
             json.dumps([v.model_dump() for v in vp.violations])))
        self._conn.commit()
        return cur.lastrowid

    def record_feedback(self, service_id: str, payload: dict) -> int:
        items = payload.get("feedback", [])
        for f in items:
            self._conn.execute(
                "INSERT INTO feedback (service_id, card_uuid, outcome, received_at, raw_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (service_id, f.get("card"), f.get("outcome"), _now(), json.dumps(f)))
        self._conn.commit()
        return len(items)

    def proposals(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM proposals")]

    def feedback(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM feedback")]
```

- [ ] **Step 3: Run tests (expect PASS), then commit**

```bash
git add src/epicvibe/audit tests/test_audit.py
git commit -m "feat: sqlite audit store for proposals and feedback"
```

---

### Task 14: CDS Hooks service (discovery, hooks, feedback, app factory)

**Files:**
- Create: `src/epicvibe/cds/hooks.py`, `src/epicvibe/cds/app.py`, `fixtures/hooks/order_select.json`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: everything from Tasks 4, 7, 8, 10, 11, 12, 13; `Settings` (Task 1); `make_provider` (Task 8).
- Produces:
  - `epicvibe.cds.app.create_app(settings: Settings | None = None, *, provider=None) -> FastAPI`. Components on `app.state`: `settings, index, engine, cache, runner, audit`.
  - Routes: `GET /cds-services` (discovery), `POST /cds-services/epicvibe-patient-view`, `POST /cds-services/epicvibe-order-select`, `POST /cds-services/epicvibe-order-sign`, `POST /cds-services/{service_id}/feedback`.
  - Service ids exactly: `epicvibe-patient-view`, `epicvibe-order-select`, `epicvibe-order-sign`. Discovery prefetch template for all three: `{"conditions": "Condition?patient={{context.patientId}}&clinical-status=active", "medications": "MedicationRequest?patient={{context.patientId}}&status=active"}`.
  - Cache key helper `cache_key(context: dict) -> str` = `encounterId` if present else `"pat:" + patientId`.
  - Behavior (Global Constraints apply): patient-view → if cached, `{"cards": [render_summary_card(vp)]}`; else summarize + enqueue generate-and-cache job + `{"cards": []}`. order-select → if cached, suggestion cards excluding codes already in `context.draftOrders`; else enqueue + `{"cards": []}`. order-sign → if cached, missing-items card (or none); else `{"cards": []}`. All handlers wrapped so any exception returns `{"cards": []}`. The background job records to audit after caching.

- [ ] **Step 1: Create fixture** — `fixtures/hooks/order_select.json`: copy `patient_view.json`, change `"hook"` to `"order-select"`, add to context:

```json
"selections": ["MedicationRequest/draft1"],
"draftOrders": {"resourceType": "Bundle", "entry": [
  {"resource": {"resourceType": "MedicationRequest", "id": "draft1", "status": "draft", "intent": "order",
    "medicationCodeableConcept": {"coding": [{"system": "urn:com.epic.cdshooks.action.code.system.preference-list-item", "code": "PREF_MET_500", "display": "metFORMIN 500 mg tablet BID"}]},
    "subject": {"reference": "Patient/PAT1"}}}]}
```

- [ ] **Step 2: Write the failing test** — `tests/test_hooks.py`

```python
import json
from pathlib import Path
import httpx
from epicvibe.cds.app import create_app
from epicvibe.config import Settings
from epicvibe.inference.base import FakeProvider

GOOD = {"order_sets": [{"order_set_id": "AMB_DM2_NEWDX", "rationale": "new dx", "items": [
    {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True, "rationale": "baseline"},
    {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True, "rationale": "first-line"}]}],
    "confidence": "high"}

def _app():
    return create_app(Settings(_env_file=None, audit_db_path=":memory:"),
                      provider=FakeProvider(GOOD))

def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

def _body(name):
    return json.loads(Path(f"fixtures/hooks/{name}.json").read_text())

async def test_discovery():
    async with _client(_app()) as c:
        services = (await c.get("/cds-services")).json()["services"]
    assert {s["id"] for s in services} == {"epicvibe-patient-view", "epicvibe-order-select", "epicvibe-order-sign"}
    assert all("conditions" in s["prefetch"] for s in services)

async def test_patient_view_then_order_select_flow():
    app = _app()
    async with _client(app) as c:
        r1 = await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        assert r1.json() == {"cards": []}                    # returns before LLM
        await app.state.runner.join()                        # let background job finish
        r2 = await c.post("/cds-services/epicvibe-order-select", json=_body("order_select"))
        cards = r2.json()["cards"]
        assert len(cards) == 1
        labels = [s["label"] for s in cards[0]["suggestions"]]
        assert labels == ["Hemoglobin A1c"]                  # metformin already drafted -> excluded
        assert app.state.audit.proposals()[0]["encounter_key"] == "ENC1"

async def test_order_select_cold_cache_enqueues():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-order-select", json=_body("order_select"))
        assert r.json() == {"cards": []}
        await app.state.runner.join()
        assert app.state.cache.get("ENC1") is not None

async def test_order_sign_missing_items():
    app = _app()
    async with _client(app) as c:
        await c.post("/cds-services/epicvibe-patient-view", json=_body("patient_view"))
        await app.state.runner.join()
        body = _body("order_select")
        body["hook"] = "order-sign"
        r = await c.post("/cds-services/epicvibe-order-sign", json=body)
        assert "Hemoglobin A1c" in r.json()["cards"][0]["detail"]

async def test_handler_error_returns_empty_cards():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json={"hook": "patient-view"})
        assert r.status_code == 200 and r.json() == {"cards": []}   # no context -> swallowed

async def test_feedback_endpoint():
    app = _app()
    async with _client(app) as c:
        r = await c.post("/cds-services/epicvibe-order-select/feedback",
                         json={"feedback": [{"card": "u1", "outcome": "accepted"}]})
        assert r.status_code == 200
        assert app.state.audit.feedback()[0]["card_uuid"] == "u1"
```

Run: `.venv/Scripts/python -m pytest tests/test_hooks.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** — `src/epicvibe/cds/hooks.py`

```python
import logging

from fastapi import APIRouter, Request

from epicvibe.cds.cards import (render_missing_items_card, render_suggestion_cards,
                                render_summary_card)
from epicvibe.proposal.patient_summary import summarize_prefetch

log = logging.getLogger("epicvibe.cds")
router = APIRouter()

PREFETCH = {
    "conditions": "Condition?patient={{context.patientId}}&clinical-status=active",
    "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
}
SERVICES = [
    {"id": "epicvibe-patient-view", "hook": "patient-view",
     "title": "EpicVibe Order Review", "description": "AI order-set suggestions", "prefetch": PREFETCH},
    {"id": "epicvibe-order-select", "hook": "order-select",
     "title": "EpicVibe Order Suggestions", "description": "AI order-set suggestions", "prefetch": PREFETCH},
    {"id": "epicvibe-order-sign", "hook": "order-sign",
     "title": "EpicVibe Order Completeness", "description": "Protocol completeness check", "prefetch": PREFETCH},
]


def cache_key(context: dict) -> str:
    return context.get("encounterId") or f"pat:{context['patientId']}"


def _draft_codes(context: dict) -> frozenset[str]:
    codes = set()
    for entry in (context.get("draftOrders") or {}).get("entry", []):
        res = entry.get("resource", {})
        concept = res.get("medicationCodeableConcept") or res.get("code") or {}
        for coding in concept.get("coding", []):
            if coding.get("code"):
                codes.add(coding["code"])
    return frozenset(codes)


def _enqueue_generate(state, key: str, context: dict, prefetch: dict) -> None:
    summary = summarize_prefetch(context, prefetch)

    async def job():
        vp = await state.engine.generate(summary)
        state.cache.put(key, vp)
        state.audit.record_proposal(key, vp, model=state.settings.anthropic_model)

    state.runner.enqueue(key, job)


@router.get("/cds-services")
async def discovery() -> dict:
    return {"services": SERVICES}


@router.post("/cds-services/epicvibe-patient-view")
async def patient_view(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        key = cache_key(body["context"])
        vp = state.cache.get(key)
        if vp is not None and not vp.is_empty:
            return {"cards": [render_summary_card(vp)]}
        _enqueue_generate(state, key, body["context"], body.get("prefetch", {}))
    except Exception:
        log.exception("patient-view handler failed")
    return {"cards": []}


@router.post("/cds-services/epicvibe-order-select")
async def order_select(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        context = body["context"]
        key = cache_key(context)
        vp = state.cache.get(key)
        if vp is None:
            _enqueue_generate(state, key, context, body.get("prefetch", {}))
            return {"cards": []}
        cards = render_suggestion_cards(vp, state.index, context["patientId"],
                                        exclude_codes=_draft_codes(context))
        return {"cards": cards}
    except Exception:
        log.exception("order-select handler failed")
        return {"cards": []}


@router.post("/cds-services/epicvibe-order-sign")
async def order_sign(request: Request) -> dict:
    state = request.app.state
    try:
        body = await request.json()
        context = body["context"]
        vp = state.cache.get(cache_key(context))
        if vp is None:
            return {"cards": []}
        card = render_missing_items_card(vp, state.index, _draft_codes(context))
        return {"cards": [card] if card else []}
    except Exception:
        log.exception("order-sign handler failed")
        return {"cards": []}


@router.post("/cds-services/{service_id}/feedback")
async def feedback(service_id: str, request: Request) -> dict:
    state = request.app.state
    try:
        state.audit.record_feedback(service_id, await request.json())
    except Exception:
        log.exception("feedback handler failed")
    return {}
```

`src/epicvibe/cds/app.py`:

```python
from fastapi import FastAPI

from epicvibe.audit.store import AuditStore
from epicvibe.cache import ProposalCache
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.cds.hooks import router
from epicvibe.config import Settings
from epicvibe.inference.base import InferenceProvider
from epicvibe.inference.factory import make_provider
from epicvibe.jobs import JobRunner
from epicvibe.proposal.engine import ProposalEngine


def create_app(settings: Settings | None = None, *,
               provider: InferenceProvider | None = None) -> FastAPI:
    settings = settings or Settings()
    index = CatalogIndex(load_catalog(settings.catalog_path))
    app = FastAPI(title="EpicVibe Ordering")
    app.state.settings = settings
    app.state.index = index
    app.state.engine = ProposalEngine(index, provider or make_provider(settings))
    app.state.cache = ProposalCache(settings.cache_ttl_seconds)
    app.state.runner = JobRunner()
    app.state.audit = AuditStore(settings.audit_db_path)
    app.include_router(router)
    return app
```

- [ ] **Step 4: Run tests (expect PASS)**

Run: `.venv/Scripts/python -m pytest tests/test_hooks.py -v` — Expected: 6 passed. Also run the full suite: `.venv/Scripts/python -m pytest -q` — all green.

- [ ] **Step 5: Commit**

```bash
git add src/epicvibe/cds fixtures/hooks/order_select.json tests/test_hooks.py
git commit -m "feat: CDS Hooks service with async precompute flow"
```

---

### Task 15: Epic JWT auth (toggleable)

**Files:**
- Create: `src/epicvibe/cds/auth.py`
- Modify: `src/epicvibe/cds/hooks.py` (add router dependency), `src/epicvibe/cds/app.py` (no change needed — settings already on state)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `Settings.verify_jwt/jwks_url/jwt_audience` (Task 1).
- Produces: `epicvibe.cds.auth` —
  - `_signing_key(token: str, jwks_url: str)` (module-level for test monkeypatching; uses `jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key`)
  - `verify_epic_jwt(token: str, jwks_url: str, audience: str) -> dict` — `jwt.decode(..., algorithms=["RS256","RS384","ES256"], audience=audience)`.
  - `async def epic_auth(request: Request) -> None` FastAPI dependency: no-op when `verify_jwt` is False; else require `Authorization: Bearer <token>` and verify, raising `HTTPException(401)` on any failure. Applied to the hooks router via `APIRouter(dependencies=[Depends(epic_auth)])`.

- [ ] **Step 1: Write the failing test** — `tests/test_auth.py`

```python
import json
from pathlib import Path
import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from epicvibe.cds import auth as auth_mod
from epicvibe.cds.app import create_app
from epicvibe.config import Settings
from epicvibe.inference.base import FakeProvider

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

def _token(aud="epicvibe"):
    return pyjwt.encode({"iss": "epic", "aud": aud}, KEY, algorithm="RS256")

@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch):
    monkeypatch.setattr(auth_mod, "_signing_key", lambda token, jwks_url: KEY.public_key())

def _app():
    s = Settings(_env_file=None, audit_db_path=":memory:", verify_jwt=True,
                 jwks_url="https://example.org/jwks", jwt_audience="epicvibe")
    return create_app(s, provider=FakeProvider({"order_sets": [], "confidence": "low"}))

def _body():
    return json.loads(Path("fixtures/hooks/patient_view.json").read_text())

async def test_valid_token_accepted():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body(),
                         headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200

async def test_missing_token_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body())
    assert r.status_code == 401

async def test_wrong_audience_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body(),
                         headers={"Authorization": f"Bearer {_token(aud='other')}"})
    assert r.status_code == 401

async def test_disabled_by_default():
    s = Settings(_env_file=None, audit_db_path=":memory:")
    app = create_app(s, provider=FakeProvider({"order_sets": [], "confidence": "low"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body())
    assert r.status_code == 200
```

Run: `.venv/Scripts/python -m pytest tests/test_auth.py -v` — Expected: FAIL.

- [ ] **Step 2: Implement** — `src/epicvibe/cds/auth.py`

```python
import logging

import jwt
from fastapi import HTTPException, Request

log = logging.getLogger("epicvibe.cds")


def _signing_key(token: str, jwks_url: str):
    return jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key


def verify_epic_jwt(token: str, jwks_url: str, audience: str) -> dict:
    key = _signing_key(token, jwks_url)
    return jwt.decode(token, key, algorithms=["RS256", "RS384", "ES256"], audience=audience)


async def epic_auth(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.verify_jwt:
        return
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401)
    try:
        verify_epic_jwt(header[7:], settings.jwks_url, settings.jwt_audience)
    except HTTPException:
        raise
    except Exception:
        log.warning("JWT verification failed")
        raise HTTPException(status_code=401)
```

In `src/epicvibe/cds/hooks.py`, change the router construction line to:

```python
from fastapi import APIRouter, Depends, Request

from epicvibe.cds.auth import epic_auth

router = APIRouter(dependencies=[Depends(epic_auth)])
```

- [ ] **Step 3: Run tests (expect PASS — full suite, auth must not break Task 14 tests), then commit**

Run: `.venv/Scripts/python -m pytest -q` — Expected: all pass.

```bash
git add src/epicvibe/cds/auth.py src/epicvibe/cds/hooks.py tests/test_auth.py
git commit -m "feat: toggleable Epic JWT validation on hook endpoints"
```

---

### Task 16: Eval harness

**Files:**
- Create: `evals/__init__.py` (empty), `evals/scoring.py`, `evals/run.py`, `evals/scenarios/dm2_new_dx.json`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `ProposalEngine` (Task 10), `summarize_prefetch` (Task 7), `make_provider` (Task 8), loader/index (Tasks 3–4).
- Produces:
  - `evals.scoring.Expected(order_set_id: str, required_items: list[str], forbidden_items: list[str] = [])`
  - `evals.scoring.ScenarioScore(name: str, grounded: bool, recall: float, precision: float)`
  - `evals.scoring.score(vp: ValidatedProposal, expected: Expected, name: str) -> ScenarioScore` — over items with `include=True` in the expected order set: recall = |included ∩ required| / |required|; precision = |included ∩ (required ∪ allowed)| / |included| where allowed = all catalog items of that set minus forbidden (i.e., only forbidden items hurt precision); grounded = no violations. Empty included → precision 0.0.
  - `evals/run.py` CLI: `python -m evals.run --scenarios evals/scenarios --catalog fixtures/catalog/sample_catalog.json` — builds provider from `Settings()`, runs every scenario JSON, prints one line per scenario plus a summary; exits 1 if any scenario has `grounded == False` (the hard gate).
- Scenario JSON format: `{"name": str, "context": {...}, "prefetch": {...}, "expected": {"order_set_id": str, "required_items": [...], "forbidden_items": [...]}}`.

- [ ] **Step 1: Create scenario** — `evals/scenarios/dm2_new_dx.json`: use the `context` and `prefetch` objects from `fixtures/hooks/patient_view.json` verbatim, plus:

```json
"name": "dm2_new_dx",
"expected": {"order_set_id": "AMB_DM2_NEWDX",
             "required_items": ["ITEM_A1C", "ITEM_LIPID", "ITEM_UMALB", "ITEM_METFORMIN"],
             "forbidden_items": []}
```

- [ ] **Step 2: Write the failing test** — `tests/test_evals.py`

```python
from pathlib import Path
from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.proposal.validation import validate_proposal
from evals.scoring import Expected, score

IDX = CatalogIndex(load_catalog(Path("fixtures/catalog/sample_catalog.json")))
EXPECTED = Expected(order_set_id="AMB_DM2_NEWDX",
                    required_items=["ITEM_A1C", "ITEM_METFORMIN"])

def _vp(items):
    return validate_proposal({"order_sets": [{"order_set_id": "AMB_DM2_NEWDX",
        "rationale": "r", "items": items}], "confidence": "high"}, IDX)

def _item(item_id, variant_id):
    return {"item_id": item_id, "variant_id": variant_id, "include": True, "rationale": "r"}

def test_perfect_score():
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_METFORMIN", "V_MET_500")]),
              EXPECTED, "t")
    assert s.grounded and s.recall == 1.0 and s.precision == 1.0

def test_partial_recall():
    s = score(_vp([_item("ITEM_A1C", "V_A1C")]), EXPECTED, "t")
    assert s.recall == 0.5 and s.precision == 1.0

def test_forbidden_hurts_precision():
    exp = Expected(order_set_id="AMB_DM2_NEWDX", required_items=["ITEM_A1C"],
                   forbidden_items=["ITEM_RETINAL"])
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_RETINAL", "V_RETINAL")]), exp, "t")
    assert s.precision == 0.5

def test_ungrounded_flagged():
    s = score(_vp([_item("ITEM_FAKE", "V_FAKE")]), EXPECTED, "t")
    assert s.grounded is False
```

Run: `.venv/Scripts/python -m pytest tests/test_evals.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** — `evals/scoring.py`

```python
from pydantic import BaseModel

from epicvibe.proposal.validation import ValidatedProposal


class Expected(BaseModel):
    order_set_id: str
    required_items: list[str]
    forbidden_items: list[str] = []


class ScenarioScore(BaseModel):
    name: str
    grounded: bool
    recall: float
    precision: float


def score(vp: ValidatedProposal, expected: Expected, name: str) -> ScenarioScore:
    included = {pi.item_id for osp in vp.proposal.order_sets
                if osp.order_set_id == expected.order_set_id
                for pi in osp.items if pi.include}
    required = set(expected.required_items)
    forbidden = set(expected.forbidden_items)
    recall = len(included & required) / len(required) if required else 1.0
    precision = (len(included - forbidden) / len(included)) if included else 0.0
    return ScenarioScore(name=name, grounded=not vp.violations,
                         recall=recall, precision=precision)
```

`evals/run.py`:

```python
import argparse
import asyncio
import json
import sys
from pathlib import Path

from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.config import Settings
from epicvibe.inference.factory import make_provider
from epicvibe.proposal.engine import ProposalEngine
from epicvibe.proposal.patient_summary import summarize_prefetch
from evals.scoring import Expected, score


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=Path, default=Path("evals/scenarios"))
    ap.add_argument("--catalog", type=Path, default=Path("fixtures/catalog/sample_catalog.json"))
    args = ap.parse_args()

    settings = Settings()
    engine = ProposalEngine(CatalogIndex(load_catalog(args.catalog)), make_provider(settings))
    scores = []
    for path in sorted(args.scenarios.glob("*.json")):
        sc = json.loads(path.read_text())
        summary = summarize_prefetch(sc["context"], sc["prefetch"])
        vp = await engine.generate(summary)
        s = score(vp, Expected.model_validate(sc["expected"]), sc["name"])
        scores.append(s)
        print(f"{s.name:30s} grounded={s.grounded} recall={s.recall:.2f} precision={s.precision:.2f}")
    if scores:
        print(f"\n{len(scores)} scenarios | grounding "
              f"{sum(s.grounded for s in scores)}/{len(scores)} | "
              f"mean recall {sum(s.recall for s in scores)/len(scores):.2f} | "
              f"mean precision {sum(s.precision for s in scores)/len(scores):.2f}")
    return 0 if all(s.grounded for s in scores) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: Run tests + harness smoke, then commit**

Run: `.venv/Scripts/python -m pytest tests/test_evals.py -v` — Expected: PASS.
Run: `.venv/Scripts/python -m evals.run` — Expected: one line for `dm2_new_dx` with recall 0.00 (FakeProvider returns empty — that's fine, exit code 0 because grounding holds).

```bash
git add evals tests/test_evals.py
git commit -m "feat: eval harness with grounding hard gate"
```

---

### Task 17: Sandbox runbook + dev server

**Files:**
- Create: `docs/runbook-sandbox.md`, `README.md`

**Interfaces:**
- Consumes: the finished app (Task 14+).
- Produces: documentation only. No code changes.

- [ ] **Step 1: Write `README.md`** — short: what this is (one paragraph referencing the spec), setup (`python -m venv .venv`, `pip install -e ".[dev]"`, copy `.env.example` → `.env`), run tests (`python -m pytest`), run server (`uvicorn epicvibe.cds.app:create_app --factory --port 8000`), run evals (`python -m evals.run`).

- [ ] **Step 2: Write `docs/runbook-sandbox.md`** with these sections:

1. **Local smoke:** start server; `curl http://localhost:8000/cds-services` shows 3 services; POST `fixtures/hooks/patient_view.json` then `fixtures/hooks/order_select.json` to the respective endpoints and see suggestion cards (set `EPICVIBE_INFERENCE_PROVIDER=anthropic` + API key for real proposals).
2. **CDS Hooks Sandbox:** expose the local server with a tunnel (`cloudflared tunnel --url http://localhost:8000` or ngrok); open https://sandbox.cds-hooks.org, add the tunnel URL as a CDS service endpoint, select a patient, verify cards render on patient-view and order-select; note the sandbox sends its own FHIR test data — expect shortlist misses unless the patient has a diabetes condition; keep `EPICVIBE_VERIFY_JWT=false` here.
3. **Epic FHIR sandbox (payload fidelity):** register a free app on fhir.epic.com; use its test patients (e.g., Camila Lopez) to compare real Epic R4 Condition/MedicationRequest JSON against our fixtures; adjust fixtures where shapes differ.
4. **Customer non-prod (the real milestone):** checklist — client ID registered; CDS service endpoint URIs registered exactly; analysts build OPA record for the three hooks + prefetch templates (give them the discovery `prefetch` block verbatim); `EPICVIBE_VERIFY_JWT=true` with Epic's jwks URL; confirm patient-view fires at chart open (warm-up requirement, spec §6); measure handler latency.
5. **Known limitation:** Epic public sandbox cannot fire CDS Hooks; do not attempt.

- [ ] **Step 3: Verify the local-smoke section by following it** (server starts, discovery returns 3 services, hook POST returns `{"cards": []}` then cards after cache warm). Then commit:

```bash
git add README.md docs/runbook-sandbox.md
git commit -m "docs: README and sandbox runbook"
```

---

## Plan Self-Review (completed)

- **Spec coverage:** catalog loader + tolerance (§5) → Tasks 2–5; proposal schema/engine/grounding (§5) → Tasks 6, 9, 10; latency architecture (§6) → Tasks 11, 14 (LLM only in background job); emitters incl. advisory values in markdown (§5, §9 stage 1) → Task 12; audit + feedback (§5, §10) → Tasks 13–14; auth + fail-silent error handling (§7) → Tasks 14–15; evals with grounding hard gate (§10) → Task 16; sandbox/rollout steps 1–2 (§11) → Task 17. Deferred by spec (§8, §9 stages 2–5, §12): ambient input, richer pre-fill channels, SMART companion — no tasks, correct.
- **Type consistency:** `ValidatedProposal.is_empty`, `CatalogIndex.get_item -> tuple`, `cache_key`, service ids, and Epic code-system strings checked for exact-match usage across tasks.
- **Placeholders:** none; every code step contains complete code.
