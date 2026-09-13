"""Two-step downtime extraction engine.

Step 1 picks an order template from the offline library. Step 2 fills that
template's fields from the transcript and nothing else - during a downtime
there is no chart to reconcile against, so an invented value is strictly worse
than a blank one. Everything the model returns is then post-validated against
the template: unknown ids are dropped (with a warning), values are coerced to
the field's declared kind, and required fields left empty become `unresolved`
so the capture UI can outline them in red.
"""

import json
import re

from epicvibe.downtime.providers import (
    SPEC_CLOSE,
    SPEC_OPEN,
    TEMPLATES_CLOSE,
    TEMPLATES_OPEN,
    TRANSCRIPT_CLOSE,
    TRANSCRIPT_OPEN,
)
from epicvibe.downtime.schema import (
    FilledField,
    FilledOrder,
    FilledTemplate,
    GenerateResult,
    TemplateSelection,
    strict_json_schema,
)
from epicvibe.downtime.templates import OrderTemplate, TemplateField, TemplateLibrary

SELECT_SYSTEM = """You select an order template for a clinician working during a hospital EHR downtime.

You are given the downtime template library (id, name, setting, indications, keywords) and an
ambient transcript of the encounter. Choose the ONE template that best matches the clinical
presentation and the plan the clinician states.

Rules:
- Choose only from the template ids provided. Never invent an id.
- Weigh the stated plan (the orders the clinician actually calls for) above the chief complaint.
- Set confidence: high when the presentation and plan both clearly match; medium when the
  presentation matches but the plan is thin; low when you are guessing.
- List up to three plausible alternatives with a one-line reason each, so the clinician can switch.
- Give a rationale that cites what in the transcript drove the choice."""

FILL_SYSTEM = """You fill a downtime order template from an ambient encounter transcript.

The EHR is DOWN. There is no chart, no prior labs, no demographics feed. The transcript is the
only source of truth. A blank field is safe; an invented field is a patient-safety event.

Rules:
1. Fill a field ONLY from information stated in the transcript. If it is not stated, set value
   to null. Do not infer, do not use general medical knowledge to guess a value, do not carry a
   value over from a similar patient.
2. For every non-null value, put the VERBATIM transcript quote that justifies it in `evidence`.
   Quote exactly - do not paraphrase. If you cannot quote it, you cannot fill it.
3. Set confidence per field: high when the transcript states the value explicitly, medium when
   it requires a simple conversion (pounds to kg, "twice daily" to BID), low otherwise.
4. Select an order when the clinician indicates it. Do not select an order the clinician did not
   ask for, unless it is marked default_selected AND it is consistent with the stated plan.
   Explicitly DESELECT a default order the clinician defers ("hold the...", "no troponin for now").
   Give a one-line rationale for every order, selected or not.
5. Order fields (dose, route, frequency, duration, priority, indication, specimen notes): fill
   from the transcript first; fall back to the template `defaults` only when the transcript is
   silent, and in that case leave `evidence` null and confidence low.
6. Use only field_ids and order_ids present in the template spec.
7. List anything clinically important that you could not resolve in `unresolved`, and any
   ambiguity or apparent contradiction in `warnings`."""


class DowntimeEngine:
    def __init__(self, library: TemplateLibrary, provider):
        self.library = library
        self.provider = provider

    # -- public API ---------------------------------------------------------

    async def generate(self, transcript: str, template_id: str | None = None) -> GenerateResult:
        """Select (unless pinned) then fill. `template_id` pins the template so
        the UI's 'switch template' control can re-run only step 2."""
        if template_id:
            template = self.library.get(template_id)
            if template is None:
                raise ValueError(f"unknown template_id: {template_id}")
            selection = TemplateSelection(
                template_id=template_id,
                confidence="high",
                rationale="template chosen by the clinician",
                alternatives=[],
            )
        else:
            selection = await self.select(transcript)
            template = self.library.get(selection.template_id)
            if template is None:  # defensive; select() already validates
                raise ValueError(f"unknown template_id: {selection.template_id}")

        filled = await self.fill(transcript, template)
        return GenerateResult(selection=selection, filled=filled)

    async def select(self, transcript: str) -> TemplateSelection:
        user = (
            f"{TEMPLATES_OPEN}\n{json.dumps(self.library.summaries(), indent=2)}\n{TEMPLATES_CLOSE}\n\n"
            f"{TRANSCRIPT_OPEN}\n{transcript.strip()}\n{TRANSCRIPT_CLOSE}"
        )
        raw = await self.provider.complete_json(
            system=SELECT_SYSTEM, user=user, json_schema=strict_json_schema(TemplateSelection)
        )
        selection = TemplateSelection.model_validate(raw)
        valid = set(self.library.ids())
        if selection.template_id not in valid:
            # Unknown id from the model: fall back to the keyword shortlist rather
            # than handing the UI a template that does not exist.
            best, _score = self.library.shortlist(transcript, limit=1)[0]
            selection = selection.model_copy(update={
                "template_id": best.template_id,
                "confidence": "low",
                "rationale": f"model returned an unknown template id; fell back to keyword match. "
                             f"Original rationale: {selection.rationale}",
            })
        selection.alternatives = [a for a in selection.alternatives
                                  if a.template_id in valid and a.template_id != selection.template_id]
        return selection

    async def fill(self, transcript: str, template: OrderTemplate) -> FilledTemplate:
        user = (
            f"{SPEC_OPEN}\n{json.dumps(template.spec(), indent=2)}\n{SPEC_CLOSE}\n\n"
            f"{TRANSCRIPT_OPEN}\n{transcript.strip()}\n{TRANSCRIPT_CLOSE}"
        )
        raw = await self.provider.complete_json(
            system=FILL_SYSTEM, user=user, json_schema=strict_json_schema(FilledTemplate)
        )
        raw.setdefault("template_id", template.template_id)
        filled = FilledTemplate.model_validate(raw)
        return validate_filled(filled, template)


# ---------------------------------------------------------------------------
# post-validation
# ---------------------------------------------------------------------------


def validate_filled(filled: FilledTemplate, template: OrderTemplate) -> FilledTemplate:
    """Reconcile a model-produced fill against the template it claims to fill."""
    warnings = list(filled.warnings)
    if filled.template_id != template.template_id:
        warnings.append(
            f"fill claimed template_id {filled.template_id!r}; forced to {template.template_id!r}"
        )

    known_patient = {f.field_id: f for f in template.patient_fields}
    patient_fields: list[FilledField] = []
    seen: set[str] = set()
    for f in filled.patient_fields:
        spec = known_patient.get(f.field_id)
        if spec is None:
            warnings.append(f"dropped unknown patient field {f.field_id!r}")
            continue
        if f.field_id in seen:
            warnings.append(f"dropped duplicate patient field {f.field_id!r}")
            continue
        seen.add(f.field_id)
        patient_fields.append(_coerce(f, spec, warnings))
    # Keep template order and include fields the model omitted, so the UI always
    # renders the full capture form.
    by_id = {f.field_id: f for f in patient_fields}
    patient_fields = [
        by_id.get(spec.field_id, FilledField(field_id=spec.field_id, value=None,
                                             evidence=None, confidence="low"))
        for spec in template.patient_fields
    ]

    known_orders = {o.order_id: o for o in template.orders}
    orders_by_id: dict[str, FilledOrder] = {}
    for o in filled.orders:
        spec = known_orders.get(o.order_id)
        if spec is None:
            warnings.append(f"dropped unknown order {o.order_id!r}")
            continue
        if o.order_id in orders_by_id:
            warnings.append(f"dropped duplicate order {o.order_id!r}")
            continue
        known_fields = {f.field_id: f for f in spec.fields}
        fields: list[FilledField] = []
        fseen: set[str] = set()
        for f in o.fields:
            fspec = known_fields.get(f.field_id)
            if fspec is None:
                warnings.append(f"dropped unknown field {f.field_id!r} on order {o.order_id!r}")
                continue
            if f.field_id in fseen:
                continue
            fseen.add(f.field_id)
            fields.append(_coerce(f, fspec, warnings, order_id=o.order_id))
        present = {f.field_id for f in fields}
        for fspec in spec.fields:
            if fspec.field_id not in present:
                fields.append(FilledField(field_id=fspec.field_id, value=None,
                                          evidence=None, confidence="low"))
        fields.sort(key=lambda f: [s.field_id for s in spec.fields].index(f.field_id))
        orders_by_id[o.order_id] = FilledOrder(
            order_id=o.order_id, selected=o.selected, rationale=o.rationale, fields=fields
        )
    orders = [
        orders_by_id.get(spec.order_id, FilledOrder(
            order_id=spec.order_id,
            selected=False,
            rationale="omitted by the extraction step",
            fields=[FilledField(field_id=f.field_id, value=None, evidence=None, confidence="low")
                    for f in spec.fields],
        ))
        for spec in template.orders
    ]

    unresolved = [u for u in filled.unresolved if u]
    for spec in template.patient_fields:
        if not spec.required:
            continue
        value = next((f.value for f in patient_fields if f.field_id == spec.field_id), None)
        if not value and spec.field_id not in unresolved:
            unresolved.append(spec.field_id)
    for order in orders:
        if not order.selected:
            continue
        ospec = known_orders[order.order_id]
        for fspec in ospec.fields:
            if not fspec.required:
                continue
            value = next((f.value for f in order.fields if f.field_id == fspec.field_id), None)
            key = f"{order.order_id}.{fspec.field_id}"
            if not value and key not in unresolved:
                unresolved.append(key)

    return FilledTemplate(
        template_id=template.template_id,
        patient_fields=patient_fields,
        orders=orders,
        unresolved=unresolved,
        warnings=warnings,
    )


def _coerce(
    field: FilledField, spec: TemplateField, warnings: list[str], order_id: str | None = None
) -> FilledField:
    """Coerce a value to the field's declared kind. Unusable values are blanked
    rather than passed through - a garbled dose must show as a gap."""
    where = f"{order_id}.{field.field_id}" if order_id else field.field_id
    value = field.value
    if value is None:
        return field
    value = str(value).strip()
    if not value:
        return field.model_copy(update={"value": None})

    if spec.kind == "number":
        m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if not m:
            warnings.append(f"blanked non-numeric value {value!r} for {where}")
            return field.model_copy(update={"value": None, "confidence": "low"})
        value = m.group(0)
    elif spec.kind == "boolean":
        low = value.lower()
        if low in {"true", "yes", "y", "1"}:
            value = "true"
        elif low in {"false", "no", "n", "0"}:
            value = "false"
        else:
            warnings.append(f"blanked non-boolean value {value!r} for {where}")
            return field.model_copy(update={"value": None, "confidence": "low"})
    elif spec.kind == "date":
        m = re.search(r"\d{4}-\d{2}-\d{2}", value)
        if m:
            value = m.group(0)
    elif spec.kind == "choice" and spec.choices:
        match = next((c for c in spec.choices if c.lower() == value.lower()), None)
        if match is None:
            match = next((c for c in spec.choices if c.lower() in value.lower()), None)
        if match is None:
            warnings.append(
                f"value {value!r} for {where} is not one of {spec.choices}; kept as free text"
            )
        else:
            value = match

    return field.model_copy(update={"value": value})
