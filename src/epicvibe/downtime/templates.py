"""Downtime order templates: the offline order-set library.

A template is the downtime analogue of an Epic order set: it names the patient
demographics we must capture by hand (no EHR to read them from) plus the orders
a clinician would place for that presentation, each with its own detail fields.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

FieldKind = Literal["text", "number", "date", "choice", "boolean"]
OrderCategory = Literal[
    "lab", "imaging", "medication", "nursing", "consult", "diet", "activity"
]
Setting = Literal["ED", "inpatient", "ambulatory"]


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="allow")


class TemplateField(_Tolerant):
    field_id: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    choices: list[str] | None = None
    unit: str | None = None
    hint: str = ""


class TemplateOrder(_Tolerant):
    order_id: str
    category: OrderCategory
    display: str
    code: str
    code_system: str
    default_selected: bool = False
    fields: list[TemplateField] = []
    defaults: dict[str, str] = {}


class OrderTemplate(_Tolerant):
    template_id: str
    name: str
    setting: Setting
    description: str = ""
    indications: list[str] = []
    keywords: list[str] = []
    patient_fields: list[TemplateField] = []
    orders: list[TemplateOrder] = []

    def order(self, order_id: str) -> TemplateOrder | None:
        return next((o for o in self.orders if o.order_id == order_id), None)

    def summary(self) -> dict:
        """Compact form handed to the model for template selection."""
        return {
            "template_id": self.template_id,
            "name": self.name,
            "setting": self.setting,
            "indications": self.indications,
            "keywords": self.keywords,
        }

    def spec(self) -> dict:
        """Full field spec handed to the model for template filling."""
        def _f(f: TemplateField) -> dict:
            d = {"field_id": f.field_id, "label": f.label, "kind": f.kind,
                 "required": f.required}
            if f.choices:
                d["choices"] = f.choices
            if f.unit:
                d["unit"] = f.unit
            if f.hint:
                d["hint"] = f.hint
            return d

        return {
            "template_id": self.template_id,
            "name": self.name,
            "setting": self.setting,
            "patient_fields": [_f(f) for f in self.patient_fields],
            "orders": [
                {
                    "order_id": o.order_id,
                    "category": o.category,
                    "display": o.display,
                    "default_selected": o.default_selected,
                    "fields": [_f(f) for f in o.fields],
                    "defaults": o.defaults,
                }
                for o in self.orders
            ],
        }


class TemplateLibrary:
    def __init__(self, templates: list[OrderTemplate]):
        self.templates = list(templates)
        self._by_id = {t.template_id: t for t in self.templates}

    def __len__(self) -> int:
        return len(self.templates)

    def __iter__(self):
        return iter(self.templates)

    def get(self, template_id: str) -> OrderTemplate | None:
        return self._by_id.get(template_id)

    def ids(self) -> list[str]:
        return [t.template_id for t in self.templates]

    def summaries(self) -> list[dict]:
        return [t.summary() for t in self.templates]

    def shortlist(self, text: str, limit: int = 5) -> list[tuple[OrderTemplate, int]]:
        """Keyword-overlap shortlist, best first. Score = matched keyword count."""
        low = text.lower()
        scored: list[tuple[OrderTemplate, int]] = []
        for t in self.templates:
            terms = {k.lower() for k in t.keywords} | {i.lower() for i in t.indications}
            score = sum(1 for term in terms if term and term in low)
            if t.name.lower() in low:
                score += 2
            scored.append((t, score))
        scored.sort(key=lambda p: (-p[1], p[0].template_id))
        return scored[:limit]


def load_templates(directory: Path | str) -> TemplateLibrary:
    """Load every *.json order template in `directory`."""
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"template directory not found: {d}")
    templates: list[OrderTemplate] = []
    for path in sorted(d.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        templates.append(OrderTemplate.model_validate(data))
    seen: set[str] = set()
    for t in templates:
        if t.template_id in seen:
            raise ValueError(f"duplicate template_id: {t.template_id}")
        seen.add(t.template_id)
    return TemplateLibrary(templates)
