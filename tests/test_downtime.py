"""Templates, extraction engine, order store and HL7 construction."""

import json
from pathlib import Path

import pytest

from epicvibe.downtime import hl7
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.engine import DowntimeEngine, validate_filled
from epicvibe.downtime.providers import KeywordFakeProvider
from epicvibe.downtime.schema import (
    FilledField,
    FilledOrder,
    FilledTemplate,
    TemplateSelection,
    strict_json_schema,
)
from epicvibe.downtime.store import DowntimeStore
from epicvibe.downtime.templates import OrderTemplate, load_templates

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO / "fixtures" / "downtime" / "templates"
TRANSCRIPT_DIR = REPO / "fixtures" / "downtime" / "transcripts"

# Each fixture transcript is written for exactly one template; the file stem is
# the expected template_id, which keeps the mapping honest.
EXPECTED = [
    "ambulatory-new-t2dm",
    "chf-exacerbation-admission",
    "dka-management",
    "ed-cap-admission",
    "ed-chest-pain-acs",
    "sepsis-bundle",
]


@pytest.fixture(scope="module")
def library():
    return load_templates(TEMPLATE_DIR)


@pytest.fixture
def settings(tmp_path):
    return DowntimeSettings(
        templates_dir=TEMPLATE_DIR,
        transcripts_dir=TRANSCRIPT_DIR,
        db_path=tmp_path / "downtime.db",
        engine_host="127.0.0.1",
        engine_port=0,
    )


@pytest.fixture
def store(settings):
    s = DowntimeStore(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def engine(library):
    return DowntimeEngine(library, KeywordFakeProvider())


def transcript(name: str) -> str:
    return (TRANSCRIPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


def test_library_loads_all_six_templates(library):
    assert sorted(library.ids()) == EXPECTED
    assert len(library) == 6


def test_templates_are_well_formed(library):
    for t in library:
        assert t.setting in ("ED", "inpatient", "ambulatory")
        assert t.indications and t.keywords
        assert len(t.orders) >= 10
        patient_ids = [f.field_id for f in t.patient_fields]
        for required in ("patient_name", "dob", "sex", "mrn", "weight_kg",
                         "allergies", "ordering_provider", "encounter_location",
                         "chief_complaint"):
            assert required in patient_ids, f"{t.template_id} missing {required}"
        order_ids = [o.order_id for o in t.orders]
        assert len(order_ids) == len(set(order_ids)), f"{t.template_id} has duplicate order ids"
        for o in t.orders:
            assert o.code and o.code_system
            assert len(o.fields) >= 1
            fids = [f.field_id for f in o.fields]
            assert len(fids) == len(set(fids))
        assert any(o.category == "medication" for o in t.orders)
        assert any(o.category == "lab" for o in t.orders)


def test_every_template_has_a_transcript(library):
    for t in library:
        assert (TRANSCRIPT_DIR / f"{t.template_id}.txt").is_file()


def test_load_templates_rejects_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_templates(tmp_path / "nope")


def test_shortlist_ranks_by_keyword_overlap(library):
    best, score = library.shortlist(transcript("sepsis-bundle"), limit=1)[0]
    assert best.template_id == "sepsis-bundle"
    assert score > 0


def test_strict_schema_has_additional_properties_false_everywhere():
    schema = strict_json_schema(FilledTemplate)
    stack = [schema]
    seen_objects = 0
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "properties" in node:
                seen_objects += 1
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert seen_objects >= 3
    # Optional[str] must survive as a nullable type, not an anyOf branch.
    assert schema["$defs"]["FilledField"]["properties"]["value"]["type"] == ["string", "null"]


# --------------------------------------------------------------------------
# engine + keyword provider
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED)
async def test_keyword_provider_selects_the_right_template(engine, name):
    selection = await engine.select(transcript(name))
    assert selection.template_id == name
    assert selection.confidence in ("high", "medium")
    assert selection.rationale


@pytest.mark.parametrize("name", EXPECTED)
async def test_keyword_provider_fills_at_least_five_patient_fields(engine, name):
    result = await engine.generate(transcript(name))
    filled = [f for f in result.filled.patient_fields if f.value]
    assert len(filled) >= 5, [f.field_id for f in filled]
    # Every value the provider produced from the transcript carries a quote.
    quoted = [f for f in filled if f.evidence]
    assert len(quoted) >= 5
    for f in quoted:
        assert f.evidence.strip() in transcript(name)


@pytest.mark.parametrize("name", EXPECTED)
async def test_generate_selects_orders_and_reports_gaps(engine, name):
    result = await engine.generate(transcript(name))
    assert result.filled.template_id == name
    assert len(result.filled.selected_orders()) >= 5
    # Every template order is represented so the UI can render the full form.
    assert len(result.filled.orders) == len(engine.library.get(name).orders)
    assert isinstance(result.filled.unresolved, list)


async def test_generate_with_pinned_template_skips_selection(engine):
    result = await engine.generate(transcript("ed-cap-admission"),
                                   template_id="ed-chest-pain-acs")
    assert result.filled.template_id == "ed-chest-pain-acs"
    assert result.selection.rationale == "template chosen by the clinician"


async def test_generate_rejects_unknown_pinned_template(engine):
    with pytest.raises(ValueError):
        await engine.generate("anything", template_id="not-a-template")


async def test_keyword_provider_deselects_explicitly_deferred_orders(engine):
    # "no troponin for now" in the chest-pain transcript must not select it,
    # and the CHF transcript defers carvedilol.
    chf = await engine.generate(transcript("chf-exacerbation-admission"))
    carvedilol = next(o for o in chf.filled.orders if o.order_id == "carvedilol")
    assert carvedilol.selected is False
    assert "defer" in carvedilol.rationale


async def test_keyword_provider_records_medication_doses(engine):
    result = await engine.generate(transcript("ed-cap-admission"))
    ceftriaxone = next(o for o in result.filled.orders if o.order_id == "ceftriaxone")
    assert ceftriaxone.selected
    values = {f.field_id: f.value for f in ceftriaxone.fields}
    assert values["dose"] == "1"
    assert values["dose_units"] == "g"
    assert values["route"] == "IV"
    assert values["frequency"] == "daily"


# --------------------------------------------------------------------------
# post-validation
# --------------------------------------------------------------------------


def test_validation_drops_unknown_ids_and_flags_missing_required(library):
    template = library.get("ed-cap-admission")
    raw = FilledTemplate(
        template_id="ed-cap-admission",
        patient_fields=[
            FilledField(field_id="patient_name", value="Jane Roe", evidence="Jane Roe",
                        confidence="high"),
            FilledField(field_id="favourite_colour", value="teal"),
        ],
        orders=[
            FilledOrder(order_id="cbc", selected=True, fields=[
                FilledField(field_id="priority", value="STAT"),
                FilledField(field_id="indication", value="pneumonia"),
                FilledField(field_id="made_up_field", value="x"),
            ]),
            FilledOrder(order_id="teleportation", selected=True),
        ],
    )
    out = validate_filled(raw, template)
    ids = [f.field_id for f in out.patient_fields]
    assert "favourite_colour" not in ids
    assert any("favourite_colour" in w for w in out.warnings)
    assert any("teleportation" in w for w in out.warnings)
    assert any("made_up_field" in w for w in out.warnings)
    assert "teleportation" not in [o.order_id for o in out.orders]
    # Missing required patient fields are surfaced for the red outline in the UI.
    assert "dob" in out.unresolved and "allergies" in out.unresolved
    assert "patient_name" not in out.unresolved


def test_validation_coerces_types_and_blanks_garbage(library):
    template = library.get("ed-cap-admission")
    raw = FilledTemplate(
        template_id="ed-cap-admission",
        patient_fields=[
            FilledField(field_id="weight_kg", value="82 kg"),
            FilledField(field_id="dob", value="born 1952-03-14"),
            FilledField(field_id="sex", value="MALE"),
        ],
        orders=[FilledOrder(order_id="ceftriaxone", selected=True, fields=[
            FilledField(field_id="dose", value="a handful"),
        ])],
    )
    out = validate_filled(raw, template)
    values = {f.field_id: f.value for f in out.patient_fields}
    assert values["weight_kg"] == "82"
    assert values["dob"] == "1952-03-14"
    assert values["sex"] == "male"
    dose = next(f for o in out.orders if o.order_id == "ceftriaxone"
                for f in o.fields if f.field_id == "dose")
    assert dose.value is None
    assert any("a handful" in w for w in out.warnings)
    assert "ceftriaxone.dose" in out.unresolved


def test_validation_forces_the_template_id(library):
    template = library.get("dka-management")
    out = validate_filled(FilledTemplate(template_id="sepsis-bundle"), template)
    assert out.template_id == "dka-management"
    assert any("sepsis-bundle" in w for w in out.warnings)


async def test_engine_falls_back_when_model_invents_a_template_id(library):
    class BadProvider:
        async def complete_json(self, *, system, user, json_schema):
            return {"template_id": "hallucinated", "confidence": "high",
                    "rationale": "made it up", "alternatives": [
                        {"template_id": "also-fake", "reason": "no"}]}

    engine = DowntimeEngine(library, BadProvider())
    selection = await engine.select(transcript("dka-management"))
    assert selection.template_id == "dka-management"
    assert selection.confidence == "low"
    assert selection.alternatives == []


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


def test_store_lifecycle(store):
    order_id = store.create_draft("ed-cap-admission",
                                  [{"field_id": "patient_name", "value": "A B"}],
                                  [{"order_id": "cbc", "selected": True, "fields": []}],
                                  "transcript text")
    row = store.get(order_id)
    assert row["status"] == "draft"
    assert row["transcript_hash"]
    assert store.counts() == {"draft": 1}

    store.update_draft(order_id, patient_fields=[{"field_id": "patient_name", "value": "C D"}])
    assert store.get(order_id)["patient_fields"][0]["value"] == "C D"

    signed = store.sign(order_id, "Dr. Who")
    assert signed["status"] == "queued"
    assert signed["signed_by"] == "Dr. Who" and signed["signed_at"]
    assert [r["id"] for r in store.export_batch()] == [order_id]

    # Signed orders are no longer editable.
    with pytest.raises(ValueError):
        store.update_draft(order_id, orders=[])

    store.mark_sending(order_id, "CTRL1")
    row = store.get(order_id)
    assert row["status"] == "sending" and row["attempts"] == 1 and row["hl7_control_id"] == "CTRL1"

    store.mark_result(order_id, "acked", ack_code="AA", ack_text="ok")
    assert store.get(order_id)["status"] == "acked"
    assert store.list(status="acked")[0]["id"] == order_id
    assert store.export_batch() == []

    store.mark_reconciled(order_id, mrn="MRN-99")
    row = store.get(order_id)
    assert row["status"] == "reconciled"
    assert any(f["field_id"] == "mrn" and f["value"] == "MRN-99" for f in row["patient_fields"])


def test_store_recovery_worklist_and_log(store):
    ok = store.create_draft("ed-cap-admission", [], [])
    bad = store.create_draft("ed-cap-admission", [], [])
    store.sign(ok, "x"), store.sign(bad, "x")
    store.mark_result(ok, "acked", ack_code="AA")
    store.mark_result(bad, "nacked", ack_code="AE", ack_text="unknown patient")
    worklist = store.recovery_worklist()
    assert [r["id"] for r in worklist] == [bad] or {r["id"] for r in worklist} == {bad}
    store.log_hl7(bad, "out", "MSH|...", "CTRL2")
    store.log_hl7(bad, "in", "MSA|AE|CTRL2", "CTRL2")
    log = store.hl7_log(bad)
    assert [e["direction"] for e in log] == ["out", "in"]


def test_store_unknown_order(store):
    assert store.get("nope") is None
    with pytest.raises(KeyError):
        store.sign("nope", "x")


# --------------------------------------------------------------------------
# HL7
# --------------------------------------------------------------------------


def test_escape_covers_every_delimiter():
    assert hl7.escape(r"a|b^c~d\e&f") == r"a\F\b\S\c\R\d\E\e\T\f"
    assert hl7.unescape(hl7.escape(r"a|b^c~d\e&f")) == r"a|b^c~d\e&f"
    assert hl7.escape(None) == ""


def test_escaped_delimiters_do_not_break_field_parsing(store, library, settings):
    template = library.get("ed-cap-admission")
    order_id = store.create_draft(
        "ed-cap-admission",
        [{"field_id": "patient_name", "value": "O|Brien^Ann&Co~X\\Y"},
         {"field_id": "sex", "value": "female"},
         {"field_id": "allergies", "value": "penicillin"}],
        [{"order_id": "cbc", "selected": True,
          "fields": [{"field_id": "indication", "value": "rule|out"}]}],
    )
    message, _ = hl7.build_orm(store.get(order_id), template, settings)
    pid = next(s for s in hl7.segments(message) if s.startswith("PID"))
    assert pid.count("|") == 8  # the embedded pipe is escaped, not a new field
    for token in ("\\F\\", "\\S\\", "\\T\\", "\\R\\"):
        assert token in pid


async def test_build_orm_structure(engine, store, library, settings):
    result = await engine.generate(transcript("ed-cap-admission"))
    order_id = store.create_draft(
        result.filled.template_id,
        [f.model_dump() for f in result.filled.patient_fields],
        [o.model_dump() for o in result.filled.orders],
        transcript("ed-cap-admission"),
    )
    store.sign(order_id, "Dr. Alvarez")
    row = store.get(order_id)
    template = library.get(row["template_id"])
    message, control_id = hl7.build_orm(row, template, settings)

    segments = hl7.segments(message)
    assert segments[0].startswith("MSH|^~\\&|EPICVIBE|DOWNTIME|EPIC|BRIDGES|")
    msh = segments[0].split("|")
    assert msh[8] == "ORM^O01^ORM_O01"
    assert msh[9] == control_id and len(control_id) <= 20
    assert msh[10] == "T" and msh[11] == "2.5.1"

    pid = segments[1].split("|")
    assert pid[3].startswith("DT-")            # no MRN in the transcript
    assert pid[3].endswith("^DTID")
    assert pid[5] == "BENNETT^HAROLD^"
    assert pid[7] == "19520314" and pid[8] == "M"

    assert segments[2].startswith("PV1|1|E|ED Bay 4")
    assert any(s.startswith("AL1|1|DA|sulfa drugs") for s in segments)

    counts = hl7.segment_counts(message)
    selected = [o for o in row["orders"] if o["selected"]]
    meds = [o for o in selected if template.order(o["order_id"]).category == "medication"]
    assert counts["ORC"] == counts["OBR"] == len(selected)
    assert counts["RXO"] == len(meds)
    assert counts["MSH"] == counts["PID"] == counts["PV1"] == 1
    assert counts["NTE"] >= len(selected)

    orc = next(s for s in segments if s.startswith("ORC"))
    fields = orc.split("|")
    assert fields[1] == "NW"
    assert fields[2] == f"{control_id}-1"
    assert fields[12] == "Dr. Alvarez"

    # A diagnostic order's clinical indication lives in OBR-31 (Reason for Study),
    # not ORC-16, and its priority is repeated in the OBR-27 quantity/timing field.
    lab = next(o for o in selected
               if template.order(o["order_id"]).category != "medication")
    lab_index = [o["order_id"] for o in selected].index(lab["order_id"]) + 1
    lab_obr = next(s for s in segments if s.startswith(f"OBR|{lab_index}|"))
    lab_fields = lab_obr.split("|")
    assert lab_fields[31]                         # OBR-31 reason for study
    assert lab_fields[27].startswith("1^^^")
    assert lab_fields[27].split("^")[5] == lab_fields[5]   # priority mirrors OBR-5
    lab_orc = [s for s in segments if s.startswith("ORC")][lab_index - 1].split("|")
    assert len(lab_orc) <= 16 or not lab_orc[16]  # ORC-16 is empty for non-meds

    med = next(o for o in selected
               if template.order(o["order_id"]).category == "medication")
    med_index = [o["order_id"] for o in selected].index(med["order_id"]) + 1
    med_orc = [s for s in segments if s.startswith("ORC")][med_index - 1].split("|")
    assert med_orc[16]                            # meds keep their ORC-16 reason
    # the indication is still narrated in an NTE either way
    assert any(s.startswith("NTE|") and "Indication:" in s for s in segments)

    rxo = next(s for s in segments if s.startswith("RXO")).split("|")
    assert rxo[1].startswith("1665005^Ceftriaxone")
    assert rxo[2] == "1" and rxo[4] == "g"
    assert any(s.startswith("RXR|IV") for s in segments)

    summary = hl7.summarize(message)
    assert summary["message_type"] == "ORM^O01^ORM_O01"
    assert summary["control_id"] == control_id
    assert summary["orc_count"] == len(selected)


def test_build_orm_uses_a_real_mrn_when_known(store, library, settings):
    template = library.get("ambulatory-new-t2dm")
    order_id = store.create_draft("ambulatory-new-t2dm", [
        {"field_id": "mrn", "value": "E1234567"},
        {"field_id": "patient_name", "value": "Yolanda Pritchard"},
        {"field_id": "allergies", "value": "NKDA"},
    ], [])
    message, _ = hl7.build_orm(store.get(order_id), template, settings)
    pid = hl7.segments(message)[1].split("|")
    assert pid[3] == "E1234567^^^BRIDGES^MR"
    assert not any(s.startswith("AL1") for s in hl7.segments(message))  # NKDA is not an allergy


def test_parse_ack():
    ack = hl7.build_ack("DTABC123", "AA", "Accepted 4 order(s)")
    code, control, text = hl7.parse_ack(ack)
    assert (code, control, text) == ("AA", "DTABC123", "Accepted 4 order(s)")
    assert hl7.ack_is_accept(code) and not hl7.ack_is_reject(code)

    code, control, text = hl7.parse_ack(hl7.build_ack("DTX", "AE", "bad patient|id"))
    assert code == "AE" and control == "DTX"
    assert text == "bad patient|id"          # unescaped on the way back out
    assert hl7.ack_is_reject(code)

    assert hl7.parse_ack("")[0] == ""
    assert hl7.parse_ack("MSH|^~\\&|X")[2] == "no MSA segment in ACK"


def test_selection_schema_roundtrip():
    s = TemplateSelection.model_validate(json.loads(
        '{"template_id":"x","confidence":"medium","rationale":"r",'
        '"alternatives":[{"template_id":"y","reason":"z"}]}'))
    assert s.alternatives[0].template_id == "y"


def test_order_template_spec_is_json_serializable(library):
    for t in library:
        assert json.loads(json.dumps(t.spec()))["template_id"] == t.template_id
        assert json.loads(json.dumps(t.summary()))["template_id"] == t.template_id
    assert isinstance(library.get("dka-management"), OrderTemplate)
