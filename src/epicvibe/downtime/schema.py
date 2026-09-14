"""Structured output schema for downtime template selection and filling.

Everything the model returns is evidence-bearing: each filled value carries the
verbatim transcript quote that justifies it, because during a downtime there is
no EHR data to fall back on and no way to silently correct a hallucination.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Confidence = Literal["high", "medium", "low"]

#: Where a filled value came from. The clinician signing the order needs to see
#: this at a glance: `transcript` is something the clinician said (and the quote
#: is in `evidence`), `default` is a guideline-derived value baked into the
#: template by a human, `none` is a gap. Only `transcript` is model output.
FieldSource = Literal["transcript", "default", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TemplateAlternative(_Strict):
    template_id: str
    reason: str = ""


class TemplateSelection(_Strict):
    template_id: str
    confidence: Confidence = "low"
    rationale: str = ""
    alternatives: list[TemplateAlternative] = []


class FilledField(_Strict):
    field_id: str
    value: str | None = None
    evidence: str | None = None
    confidence: Confidence = "low"
    #: Set by post-validation, not by the model - see `engine.validate_filled`.
    #: Anything the model puts here is overwritten.
    source: FieldSource = "none"


class FilledOrder(_Strict):
    order_id: str
    selected: bool = False
    rationale: str = ""
    fields: list[FilledField] = []


class FilledTemplate(_Strict):
    template_id: str
    patient_fields: list[FilledField] = []
    orders: list[FilledOrder] = []
    unresolved: list[str] = []
    warnings: list[str] = []

    def field(self, field_id: str) -> FilledField | None:
        return next((f for f in self.patient_fields if f.field_id == field_id), None)

    def value(self, field_id: str) -> str | None:
        f = self.field(field_id)
        return f.value if f else None

    def selected_orders(self) -> list[FilledOrder]:
        return [o for o in self.orders if o.selected]


class GenerateResult(_Strict):
    """What the /api/generate endpoint hands back to the capture UI."""

    selection: TemplateSelection
    filled: FilledTemplate


def strict_json_schema(model: type[BaseModel]) -> dict:
    """pydantic JSON schema, post-processed for Anthropic `strict: true` tools.

    Strict tool schemas require every object to carry `additionalProperties:
    false` and to list every property in `required`. Pydantic emits neither for
    optional fields, so we walk the schema and fix it up. `$defs`/`$ref` are
    left intact (they are supported); anyOf-with-null (from `X | None`) is
    flattened to the non-null branch plus a nullable type list, which strict
    mode accepts.
    """
    schema = model.model_json_schema()
    _strictify(schema)
    return schema


def _strictify(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _strictify(item)
        return
    if not isinstance(node, dict):
        return

    # Flatten `anyOf: [X, {type: null}]` produced by Optional[...] fields.
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2:
        nulls = [b for b in any_of if isinstance(b, dict) and b.get("type") == "null"]
        others = [b for b in any_of if isinstance(b, dict) and b.get("type") != "null"]
        if nulls and len(others) == 1 and "type" in others[0]:
            node.pop("anyOf")
            node.update({k: v for k, v in others[0].items() if k != "type"})
            node["type"] = [others[0]["type"], "null"]

    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties")
        if isinstance(props, dict):
            node["additionalProperties"] = False
            node["required"] = list(props.keys())

    for value in node.values():
        _strictify(value)
