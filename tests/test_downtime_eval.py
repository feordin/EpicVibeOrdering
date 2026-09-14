"""The downtime eval harness: expectations, scoring, and the CLI.

The scorer is the thing that tells us whether a local model is safe to put in
front of a clinician, so it gets tested as carefully as the engine it scores -
including the cases where it must *not* give credit (a quote the model invented,
a default order the clinician deferred).
"""

import json

import pytest

from epicvibe.downtime import eval as dteval
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.engine import DowntimeEngine
from epicvibe.downtime.providers import KeywordFakeProvider
from epicvibe.downtime.schema import (
    FilledField,
    FilledOrder,
    FilledTemplate,
    GenerateResult,
    TemplateSelection,
)
from epicvibe.downtime.templates import load_templates

SETTINGS = DowntimeSettings()
EXPECTED_DIR = dteval.DEFAULT_EXPECTED_DIR


@pytest.fixture(scope="module")
def library():
    return load_templates(SETTINGS.templates_dir)


@pytest.fixture(scope="module")
def transcripts():
    return dteval.load_transcripts(SETTINGS.transcripts_dir)


@pytest.fixture(scope="module")
def expectations():
    return dteval.load_expectations(EXPECTED_DIR)


# ---------------------------------------------------------------------------
# fixtures on disk
# ---------------------------------------------------------------------------


def test_every_transcript_has_an_expectation(transcripts, expectations):
    assert set(expectations) == set(transcripts)
    assert len(expectations) == 6


def test_expectations_reference_only_real_templates_and_orders(library, expectations):
    """A typo in an expectation would silently mark a correct run as a failure."""
    for name, exp in expectations.items():
        template = library.get(exp.template_id)
        assert template is not None, f"{name}: unknown template {exp.template_id!r}"
        order_ids = {o.order_id for o in template.orders}
        field_ids = {f.field_id for f in template.patient_fields}
        assert set(exp.patient_fields) <= field_ids, f"{name}: unknown patient field"
        assert set(exp.selected_orders) <= order_ids, f"{name}: unknown selected order"
        assert set(exp.deselected_orders) <= order_ids, f"{name}: unknown deselected order"
        overlap = set(exp.selected_orders) & set(exp.deselected_orders)
        assert not overlap, f"{name}: {overlap} is both expected and deferred"


def test_expected_patient_values_really_appear_in_the_transcript(transcripts, expectations):
    """Ground truth must be derivable from the transcript alone - no outside knowledge."""
    for name, exp in expectations.items():
        haystack = dteval._norm(transcripts[name])
        for field_id, candidates in exp.patient_fields.items():
            assert any(dteval._norm(c) in haystack for c in candidates), (
                f"{name}.{field_id}: none of {candidates} appears in the transcript"
            )


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


TRANSCRIPT = (
    "NURSE: This is Mr. Harold Bennett in ED Bay 4. He weighs 82 kg.\n"
    "DR. ALVAREZ: Draw a CBC with differential. Not the viral panel right now."
)


def _result(template_id="ed-cap-admission", patient=(), orders=()):
    return GenerateResult(
        selection=TemplateSelection(template_id=template_id),
        filled=FilledTemplate(
            template_id=template_id,
            patient_fields=[FilledField(field_id=f, value=v, evidence=e)
                            for f, v, e in patient],
            orders=[FilledOrder(order_id=o, selected=sel,
                                fields=[FilledField(field_id=f, value=v, evidence=e)
                                        for f, v, e in fields])
                    for o, sel, fields in orders],
        ),
    )


def _expectation(**kwargs):
    data = {"template_id": "ed-cap-admission"}
    data.update(kwargs)
    return dteval.Expectation.from_json("t", data)


def test_a_wrong_template_fails_selection_and_skips_the_order_counters():
    """Order ids are template-scoped: scoring them against the wrong template
    would produce a meaningless 0%, so they are left out entirely."""
    exp = _expectation(selected_orders=["cbc"], deselected_orders=["resp-panel"])
    score = dteval.score_result(exp, TRANSCRIPT, _result(template_id="sepsis-bundle"), None)
    assert score.selection_correct is False
    assert (score.order_expected, score.order_selected, score.deselect_total) == (0, 0, 0)


def test_field_recall_is_a_case_insensitive_substring_match():
    exp = _expectation(patient_fields={"patient_name": "Bennett", "weight_kg": "82",
                                       "allergies": "sulfa"})
    score = dteval.score_result(exp, TRANSCRIPT, _result(patient=[
        ("patient_name", "Harold BENNETT", None),   # case + extra text: hit
        ("weight_kg", "82", None),                  # exact: hit
        ("allergies", None, None),                  # blank: miss
    ]), None)
    assert (score.field_hits, score.field_total) == (2, 3)
    assert score.field_misses == ["allergies"]
    assert score.field_recall == pytest.approx(2 / 3)


def test_a_list_of_acceptable_substrings_counts_as_one_field():
    """"No known drug allergies" and "NKDA" are the same answer; either may be
    what the model writes, and neither should be scored as a miss."""
    exp = _expectation(patient_fields={"allergies": ["nkda", "no known"]})
    assert exp.patient_fields["allergies"] == ["nkda", "no known"]
    for value in ("NKDA", "No known drug allergies"):
        score = dteval.score_result(exp, TRANSCRIPT,
                                    _result(patient=[("allergies", value, None)]), None)
        assert score.field_hits == 1, value


def test_an_invented_quote_counts_against_the_evidence_rate():
    score = dteval.score_result(_expectation(), TRANSCRIPT, _result(patient=[
        ("patient_name", "Harold Bennett", "This is Mr. Harold Bennett in ED Bay 4."),
        ("weight_kg", "82", "the patient weighs 82 kilograms"),  # never said
        ("sex", "male", None),                                   # no quote at all
    ]), None)
    assert score.evidence_total == 3
    assert score.evidence_ok == 1
    assert score.evidence_rate == pytest.approx(1 / 3)
    assert len(score.hallucinated) == 1 and "weight_kg" in score.hallucinated[0]
    assert score.unquoted == ["sex"]


def test_a_quote_that_only_differs_in_whitespace_still_counts_as_verbatim():
    score = dteval.score_result(_expectation(), TRANSCRIPT, _result(patient=[
        ("patient_name", "Harold Bennett", "This is Mr. Harold Bennett\n  in ED Bay 4."),
    ]), None)
    assert score.evidence_ok == 1 and not score.hallucinated


def test_blank_values_are_not_scored_for_evidence():
    """A blank field is the safe answer during a downtime - it is not a miss."""
    score = dteval.score_result(_expectation(), TRANSCRIPT, _result(patient=[
        ("patient_name", None, None), ("mrn", "   ", None),
    ]), None)
    assert score.evidence_total == 0
    assert score.evidence_rate == 1.0


def test_order_recall_precision_and_the_invented_set(library):
    template = library.get("ed-cap-admission")
    exp = _expectation(selected_orders=["cbc", "cmp", "cxr"])
    score = dteval.score_result(exp, TRANSCRIPT, _result(orders=[
        ("cbc", True, []),        # expected + selected
        ("cmp", True, []),        # expected + selected
        ("cxr", False, []),       # expected but missed
        ("o2", True, []),         # a template default, not expected: precision only
        ("pulm", True, []),       # neither expected nor a default: invented
    ]), template)
    assert (score.order_hits, score.order_expected, score.order_selected) == (2, 3, 4)
    assert score.order_recall == pytest.approx(2 / 3)
    assert score.order_precision == pytest.approx(2 / 4)
    assert score.missed_orders == ["cxr"]
    assert score.invented_orders == ["pulm"]


def test_deselection_is_scored_on_the_orders_the_clinician_deferred(library):
    template = library.get("ed-cap-admission")
    exp = _expectation(deselected_orders=["resp-panel", "pulm"])
    score = dteval.score_result(exp, TRANSCRIPT, _result(orders=[
        ("resp-panel", True, []),   # the clinician said "not right now": a miss
        ("pulm", False, []),        # correctly left off
    ]), template)
    assert (score.deselect_ok, score.deselect_total) == (1, 2)
    assert score.deselect_misses == ["resp-panel"]
    assert score.deselect_rate == pytest.approx(0.5)


def test_order_field_values_are_scored_for_evidence_too():
    score = dteval.score_result(_expectation(), TRANSCRIPT, _result(orders=[
        ("cbc", True, [("dose", "1", "Draw a CBC with differential."),
                       ("route", "IV", "give it intravenously")]),
    ]), None)
    assert score.evidence_total == 2 and score.evidence_ok == 1
    assert "cbc.route" in score.hallucinated[0]


# ---------------------------------------------------------------------------
# report + failure thresholds
# ---------------------------------------------------------------------------


def test_a_provider_that_raises_is_reported_not_propagated(library, expectations, transcripts):
    class Exploding:
        def describe(self):
            return "exploding"

        async def complete_json(self, **kwargs):
            raise RuntimeError("ollama is not running")

    report = dteval_run(Exploding(), library, expectations, transcripts)
    assert len(report.errors) == 6
    assert "ollama is not running" in report.scores[0].error
    assert report.selection_accuracy == 0.0


def dteval_run(provider, library, expectations, transcripts):
    import asyncio
    return asyncio.run(dteval.run_eval(provider, library, expectations, transcripts))


def test_report_json_round_trips(library, expectations, transcripts):
    report = dteval_run(KeywordFakeProvider(), library, expectations, transcripts)
    blob = json.loads(json.dumps(report.to_dict()))
    assert blob["transcripts"] == 6
    assert blob["selection_accuracy"] == 1.0
    assert {s["name"] for s in blob["scores"]} == set(expectations)


# ---------------------------------------------------------------------------
# the fake provider is the regression floor
# ---------------------------------------------------------------------------


async def test_the_fake_provider_picks_the_right_template_for_all_six(library, expectations,
                                                                     transcripts):
    """The deterministic extractor is what runs when the network is unplugged.
    If it cannot route a transcript to the right order set, the downtime demo is
    broken regardless of what any model does."""
    report = await dteval.run_eval(KeywordFakeProvider(), library, expectations, transcripts)
    wrong = [(s.name, s.actual_template) for s in report.scores if not s.selection_correct]
    assert wrong == []
    assert report.selection_accuracy == 1.0


async def test_the_fake_provider_captures_every_stated_demographic(library, expectations,
                                                                  transcripts):
    report = await dteval.run_eval(KeywordFakeProvider(), library, expectations, transcripts)
    misses = {s.name: s.field_misses for s in report.scores if s.field_misses}
    assert misses == {}
    assert report.mean_field_recall == 1.0


async def test_the_fake_provider_never_invents_a_quote(library, expectations, transcripts):
    """Its evidence is sliced straight out of the transcript, so a non-empty
    quote that is not in the transcript would be a real bug."""
    report = await dteval.run_eval(KeywordFakeProvider(), library, expectations, transcripts)
    assert [s.hallucinated for s in report.scores if s.hallucinated] == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_passes_on_the_fake_provider_and_writes_json(tmp_path, capsys):
    out = tmp_path / "report.json"
    code = dteval.main(["--provider", "fake", "--json", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "provider: fake:keyword" in printed
    assert "MEAN (n=6)" in printed
    assert "PASS" in printed
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["provider"] == "fake:keyword"
    assert blob["selection_accuracy"] == 1.0


def test_cli_only_runs_the_named_transcript(capsys):
    assert dteval.main(["--provider", "fake", "--only", "sepsis-bundle"]) == 0
    printed = capsys.readouterr().out
    assert "transcripts: 1" in printed
    assert "sepsis-bundle" in printed
    assert "dka-management" not in printed


def test_cli_rejects_an_unknown_transcript_name(capsys):
    assert dteval.main(["--provider", "fake", "--only", "no-such-transcript"]) == 2
    assert "unknown transcript" in capsys.readouterr().err


def test_cli_exits_non_zero_when_field_recall_is_below_the_threshold(capsys):
    """The gate is what makes this usable in CI: a model that regresses fails."""
    assert dteval.main(["--provider", "fake", "--min-field-recall", "1.01"]) == 1
    assert "mean patient-field recall" in capsys.readouterr().out


def test_cli_routes_model_to_the_right_setting():
    args = dteval.build_parser().parse_args(["--provider", "ollama", "--model", "qwen2.5:7b"])
    assert dteval._settings(args).ollama_model == "qwen2.5:7b"
    args = dteval.build_parser().parse_args(["--provider", "anthropic", "--model", "claude-x"])
    assert dteval._settings(args).model == "claude-x"
