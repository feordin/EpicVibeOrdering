"""Offline eval harness for the downtime extraction engine.

    python -m epicvibe.downtime.eval --provider fake
    python -m epicvibe.downtime.eval --provider ollama --model qwen2.5:7b --json out.json

Runs the two-step engine (template selection, then template fill) over every
transcript in `fixtures/downtime/transcripts/` and scores the result against a
hand-written expectation in `fixtures/downtime/expected/`. The expectations are
deliberately conservative: they name only what a clinician reading the
transcript would call unambiguous, so a miss is a real miss.

What is measured, and why each one matters during a downtime:

selection      Did we pick the right order set? This one is binary and it gates
               everything downstream - the wrong template means the wrong
               fields and the wrong orders, however well they are filled.
field recall   Of the demographics the clinician actually said out loud, how
               many did we capture? There is no ADT feed during a downtime, so
               anything missed here is re-keyed by hand.
evidence rate  Of the values we did fill, how many carry a verbatim quote that
               really appears in the transcript? Values filled from the
               template's own `defaults` legitimately have nothing to quote, so
               this is a floor, not a target. The number next to it - `halluc` -
               is the one that matters: a quote that is not in the transcript
               was invented, and an invented quote is worse than a blank field
               because it looks checkable and is not.
order recall   Of the orders the clinician explicitly called for, how many did
/ precision    we select? Precision is scored against the expectation, so a
               template default the clinician never mentioned counts against it;
               `invented` counts the stronger failure - a selected order that is
               neither expected nor a template default.
deselection    Of the orders the clinician explicitly deferred ("hold the
               carvedilol", "no troponin for now"), how many did we leave off?
               This is the hardest thing for a small model and the most
               dangerous to get wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.engine import DowntimeEngine
from epicvibe.downtime.providers import build_provider
from epicvibe.downtime.schema import GenerateResult
from epicvibe.downtime.templates import OrderTemplate, TemplateLibrary, load_templates

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EXPECTED_DIR = _REPO_ROOT / "fixtures" / "downtime" / "expected"


# ---------------------------------------------------------------------------
# expectations
# ---------------------------------------------------------------------------


@dataclass
class Expectation:
    """The hand-derived ground truth for one transcript."""

    name: str
    template_id: str
    #: field_id -> substring (or list of acceptable substrings) the value must contain.
    patient_fields: dict[str, list[str]] = field(default_factory=dict)
    selected_orders: list[str] = field(default_factory=list)
    deselected_orders: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_json(cls, name: str, data: dict) -> "Expectation":
        fields: dict[str, list[str]] = {}
        for field_id, wanted in (data.get("patient_fields") or {}).items():
            fields[field_id] = [wanted] if isinstance(wanted, str) else list(wanted)
        return cls(
            name=name,
            template_id=data["template_id"],
            patient_fields=fields,
            selected_orders=list(data.get("selected_orders") or []),
            deselected_orders=list(data.get("deselected_orders") or []),
            notes=data.get("notes", ""),
        )


def load_expectations(directory: Path | str) -> dict[str, Expectation]:
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"expected-results directory not found: {d}")
    out: dict[str, Expectation] = {}
    for path in sorted(d.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[path.stem] = Expectation.from_json(path.stem, data)
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    """Lowercase and collapse whitespace, so a quote that only differs in line
    wrapping still counts as verbatim."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _matches(value: str | None, wanted: Sequence[str]) -> bool:
    got = _norm(value)
    if not got:
        return False
    return any(_norm(w) in got for w in wanted)


@dataclass
class Score:
    """Scorecard for one transcript."""

    name: str
    seconds: float = 0.0
    error: str = ""

    expected_template: str = ""
    actual_template: str = ""

    field_hits: int = 0
    field_total: int = 0
    field_misses: list[str] = field(default_factory=list)

    evidence_ok: int = 0
    evidence_total: int = 0
    #: Filled values whose quote is not in the transcript. This is the
    #: patient-safety number: a quote that cannot be found was invented.
    hallucinated: list[str] = field(default_factory=list)
    #: Filled values with no quote at all. Mostly legitimate - a value that came
    #: from the template's `defaults` has nothing to quote - so it is counted
    #: separately from an invented one.
    unquoted: list[str] = field(default_factory=list)

    order_hits: int = 0
    order_expected: int = 0
    order_selected: int = 0
    missed_orders: list[str] = field(default_factory=list)
    invented_orders: list[str] = field(default_factory=list)

    deselect_ok: int = 0
    deselect_total: int = 0
    deselect_misses: list[str] = field(default_factory=list)

    @property
    def selection_correct(self) -> bool:
        return bool(self.actual_template) and self.actual_template == self.expected_template

    @property
    def field_recall(self) -> float:
        return self.field_hits / self.field_total if self.field_total else 1.0

    @property
    def evidence_rate(self) -> float:
        return self.evidence_ok / self.evidence_total if self.evidence_total else 1.0

    @property
    def hallucinated_rate(self) -> float:
        return len(self.hallucinated) / self.evidence_total if self.evidence_total else 0.0

    @property
    def order_recall(self) -> float:
        return self.order_hits / self.order_expected if self.order_expected else 1.0

    @property
    def order_precision(self) -> float:
        return self.order_hits / self.order_selected if self.order_selected else 0.0

    @property
    def deselect_rate(self) -> float:
        return self.deselect_ok / self.deselect_total if self.deselect_total else 1.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "seconds": round(self.seconds, 2),
            "error": self.error,
            "expected_template": self.expected_template,
            "actual_template": self.actual_template,
            "selection_correct": self.selection_correct,
            "field_recall": round(self.field_recall, 4),
            "field_hits": self.field_hits,
            "field_total": self.field_total,
            "field_misses": self.field_misses,
            "evidence_rate": round(self.evidence_rate, 4),
            "evidence_ok": self.evidence_ok,
            "evidence_total": self.evidence_total,
            "hallucinated_rate": round(self.hallucinated_rate, 4),
            "hallucinated": self.hallucinated[:20],
            "unquoted": self.unquoted[:20],
            "order_recall": round(self.order_recall, 4),
            "order_precision": round(self.order_precision, 4),
            "order_hits": self.order_hits,
            "order_expected": self.order_expected,
            "order_selected": self.order_selected,
            "missed_orders": self.missed_orders,
            "invented_orders": self.invented_orders,
            "deselect_rate": round(self.deselect_rate, 4),
            "deselect_ok": self.deselect_ok,
            "deselect_total": self.deselect_total,
            "deselect_misses": self.deselect_misses,
        }


def score_result(
    expectation: Expectation,
    transcript: str,
    result: GenerateResult,
    template: OrderTemplate | None,
    seconds: float = 0.0,
) -> Score:
    """Score one engine run against its expectation."""
    s = Score(
        name=expectation.name,
        seconds=seconds,
        expected_template=expectation.template_id,
        actual_template=result.selection.template_id,
    )
    filled = result.filled
    haystack = _norm(transcript)

    # --- patient-field recall ---------------------------------------------
    for field_id, wanted in expectation.patient_fields.items():
        s.field_total += 1
        if _matches(filled.value(field_id), wanted):
            s.field_hits += 1
        else:
            s.field_misses.append(field_id)

    # --- evidence rate over every filled value ----------------------------
    checked: list[tuple[str, str | None, str | None]] = [
        (f.field_id, f.value, f.evidence) for f in filled.patient_fields
    ]
    for order in filled.orders:
        checked.extend(
            (f"{order.order_id}.{f.field_id}", f.value, f.evidence) for f in order.fields
        )
    for where, value, evidence in checked:
        if not (value or "").strip():
            continue
        s.evidence_total += 1
        quote = _norm(evidence)
        if quote and quote in haystack:
            s.evidence_ok += 1
        elif quote:
            s.hallucinated.append(f"{where}: {evidence!r}")
        else:
            s.unquoted.append(where)

    # --- order selection ---------------------------------------------------
    # Only meaningful when the right template was chosen; a different template
    # has entirely different order ids, so leave the order counters at zero
    # rather than reporting a spurious 0%.
    if s.selection_correct:
        selected = {o.order_id for o in filled.selected_orders()}
        expected = set(expectation.selected_orders)
        s.order_expected = len(expected)
        s.order_selected = len(selected)
        s.order_hits = len(selected & expected)
        s.missed_orders = sorted(expected - selected)

        defaults = {o.order_id for o in template.orders if o.default_selected} if template else set()
        s.invented_orders = sorted(selected - expected - defaults)

        for order_id in expectation.deselected_orders:
            s.deselect_total += 1
            if order_id in selected:
                s.deselect_misses.append(order_id)
            else:
                s.deselect_ok += 1

    return s


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class EvalReport:
    provider: str
    scores: list[Score]

    @property
    def selection_accuracy(self) -> float:
        return _mean([1.0 if s.selection_correct else 0.0 for s in self.scores])

    @property
    def mean_field_recall(self) -> float:
        return _mean([s.field_recall for s in self.scores])

    @property
    def mean_evidence_rate(self) -> float:
        return _mean([s.evidence_rate for s in self.scores])

    @property
    def mean_hallucinated_rate(self) -> float:
        return _mean([s.hallucinated_rate for s in self.scores])

    @property
    def mean_order_recall(self) -> float:
        return _mean([s.order_recall for s in self.scores])

    @property
    def mean_order_precision(self) -> float:
        return _mean([s.order_precision for s in self.scores])

    @property
    def mean_deselect_rate(self) -> float:
        return _mean([s.deselect_rate for s in self.scores])

    @property
    def mean_seconds(self) -> float:
        return _mean([s.seconds for s in self.scores])

    @property
    def errors(self) -> list[Score]:
        return [s for s in self.scores if s.error]

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "transcripts": len(self.scores),
            "selection_accuracy": round(self.selection_accuracy, 4),
            "mean_field_recall": round(self.mean_field_recall, 4),
            "mean_evidence_rate": round(self.mean_evidence_rate, 4),
            "mean_hallucinated_rate": round(self.mean_hallucinated_rate, 4),
            "total_hallucinated": sum(len(s.hallucinated) for s in self.scores),
            "mean_order_recall": round(self.mean_order_recall, 4),
            "mean_order_precision": round(self.mean_order_precision, 4),
            "mean_deselect_rate": round(self.mean_deselect_rate, 4),
            "mean_seconds": round(self.mean_seconds, 2),
            "total_seconds": round(sum(s.seconds for s in self.scores), 2),
            "failures": len(self.errors),
            "scores": [s.to_dict() for s in self.scores],
        }


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


async def run_one(
    engine: DowntimeEngine,
    library: TemplateLibrary,
    expectation: Expectation,
    transcript: str,
) -> Score:
    started = time.monotonic()
    try:
        result = await engine.generate(transcript)
    except Exception as exc:  # noqa: BLE001 - the harness reports, it does not crash
        elapsed = time.monotonic() - started
        return Score(
            name=expectation.name,
            seconds=elapsed,
            error=f"{type(exc).__name__}: {exc}",
            expected_template=expectation.template_id,
        )
    elapsed = time.monotonic() - started
    template = library.get(result.selection.template_id)
    return score_result(expectation, transcript, result, template, elapsed)


async def run_eval(
    provider,
    library: TemplateLibrary,
    expectations: dict[str, Expectation],
    transcripts: dict[str, str],
    on_score=None,
) -> EvalReport:
    """Run every transcript sequentially (a local model has one set of weights)."""
    engine = DowntimeEngine(library, provider)
    scores: list[Score] = []
    for name in sorted(expectations):
        transcript = transcripts.get(name)
        if transcript is None:
            continue
        score = await run_one(engine, library, expectations[name], transcript)
        scores.append(score)
        if on_score is not None:
            on_score(score)
    return EvalReport(provider=describe(provider), scores=scores)


def describe(provider) -> str:
    fn = getattr(provider, "describe", None)
    return fn() if callable(fn) else type(provider).__name__


def load_transcripts(directory: Path | str) -> dict[str, str]:
    d = Path(directory)
    if not d.is_dir():
        raise FileNotFoundError(f"transcripts directory not found: {d}")
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.txt"))}


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

_HEADERS = ("transcript", "tmpl", "fields", "evid", "halluc", "ord R/P", "desel", "secs")
_WIDTHS = (28, 5, 11, 6, 7, 11, 6, 7)


def header_line() -> str:
    return "  ".join(h.ljust(w) for h, w in zip(_HEADERS, _WIDTHS)) + "\n" + \
           "  ".join("-" * w for w in _WIDTHS)


def row(score: Score) -> str:
    if score.error:
        cells = (score.name[:28], "ERR", "-", "-", "-", "-", "-", f"{score.seconds:.1f}")
    else:
        cells = (
            score.name[:28],
            "ok" if score.selection_correct else "WRONG",
            f"{score.field_recall:.0%} {score.field_hits}/{score.field_total}",
            f"{score.evidence_rate:.0%}",
            f"{len(score.hallucinated)}",
            f"{score.order_recall:.0%}/{score.order_precision:.0%}",
            f"{score.deselect_ok}/{score.deselect_total}",
            f"{score.seconds:.1f}",
        )
    return "  ".join(c.ljust(w) for c, w in zip(cells, _WIDTHS))


def summary_line(report: EvalReport) -> str:
    cells = (
        f"MEAN (n={len(report.scores)})",
        f"{report.selection_accuracy:.0%}",
        f"{report.mean_field_recall:.0%}",
        f"{report.mean_evidence_rate:.0%}",
        f"{sum(len(s.hallucinated) for s in report.scores)}",
        f"{report.mean_order_recall:.0%}/{report.mean_order_precision:.0%}",
        f"{report.mean_deselect_rate:.0%}",
        f"{report.mean_seconds:.1f}",
    )
    return "  ".join(c.ljust(w) for c, w in zip(cells, _WIDTHS))


def detail_lines(report: EvalReport) -> list[str]:
    out: list[str] = []
    for s in report.scores:
        bits: list[str] = []
        if s.error:
            bits.append(f"error: {s.error}")
        if not s.error and not s.selection_correct:
            bits.append(f"selected {s.actual_template!r}, expected {s.expected_template!r}")
        if s.field_misses:
            bits.append("missed fields: " + ", ".join(s.field_misses))
        if s.missed_orders:
            bits.append("missed orders: " + ", ".join(s.missed_orders))
        if s.invented_orders:
            bits.append("orders selected that were neither asked for nor a template default: "
                        + ", ".join(s.invented_orders))
        if s.deselect_misses:
            bits.append("failed to deselect: " + ", ".join(s.deselect_misses))
        if s.hallucinated:
            bits.append(f"{len(s.hallucinated)} INVENTED quote(s), e.g. "
                        + "; ".join(s.hallucinated[:3]))
        if s.unquoted:
            bits.append(f"{len(s.unquoted)} filled value(s) with no quote (template defaults "
                        f"legitimately have none): " + ", ".join(s.unquoted[:6])
                        + (" ..." if len(s.unquoted) > 6 else ""))
        if bits:
            out.append(f"\n{s.name}:")
            out.extend(f"  - {b}" for b in bits)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m epicvibe.downtime.eval",
        description="Score the downtime extraction engine against the fixture transcripts.",
    )
    p.add_argument("--provider", choices=("fake", "anthropic", "ollama"),
                   help="override EPICVIBE_DOWNTIME_PROVIDER")
    p.add_argument("--model", help="model id for the chosen provider")
    p.add_argument("--base-url", help="Ollama base URL (default http://localhost:11434)")
    p.add_argument("--num-ctx", type=int, help="Ollama context window")
    p.add_argument("--timeout", type=float, help="per-request timeout in seconds (Ollama)")
    p.add_argument("--templates-dir", type=Path)
    p.add_argument("--transcripts-dir", type=Path)
    p.add_argument("--expected-dir", type=Path, default=DEFAULT_EXPECTED_DIR)
    p.add_argument("--only", action="append", default=[], metavar="NAME",
                   help="run only this transcript (repeatable)")
    p.add_argument("--json", dest="json_out", type=Path, metavar="PATH",
                   help="write the full report as JSON")
    p.add_argument("--min-selection-accuracy", type=float, default=1.0)
    p.add_argument("--min-field-recall", type=float, default=0.7)
    return p


def _settings(args: argparse.Namespace) -> DowntimeSettings:
    overrides: dict = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.templates_dir:
        overrides["templates_dir"] = args.templates_dir
    if args.transcripts_dir:
        overrides["transcripts_dir"] = args.transcripts_dir
    if args.base_url:
        overrides["ollama_base_url"] = args.base_url
    if args.num_ctx:
        overrides["ollama_num_ctx"] = args.num_ctx
    if args.timeout:
        overrides["ollama_timeout_seconds"] = args.timeout
    if args.model:
        # The model setting is per-provider, so route it to the right one.
        provider = overrides.get("provider") or DowntimeSettings().provider
        overrides["ollama_model" if provider == "ollama" else "model"] = args.model
    return DowntimeSettings(**overrides)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(args)

    library = load_templates(settings.templates_dir)
    transcripts = load_transcripts(settings.transcripts_dir)
    expectations = load_expectations(args.expected_dir)
    if args.only:
        wanted = set(args.only)
        unknown = wanted - set(expectations)
        if unknown:
            print(f"unknown transcript(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        expectations = {k: v for k, v in expectations.items() if k in wanted}

    provider = build_provider(settings)
    print(f"provider: {describe(provider)}   transcripts: {len(expectations)}", flush=True)
    print(header_line(), flush=True)

    def emit(score: Score) -> None:
        # Stream as we go: a local model can take minutes per transcript, and a
        # failure that only shows up in the summary is a failure you waited an
        # hour to read.
        print(row(score), flush=True)
        if score.error:
            print(f"    {score.error}", flush=True)

    report = asyncio.run(
        run_eval(provider, library, expectations, transcripts, on_score=emit)
    )

    print("  ".join("-" * w for w in _WIDTHS))
    print(summary_line(report))
    for line in detail_lines(report):
        print(line)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json_out}")

    failures: list[str] = []
    if report.selection_accuracy < args.min_selection_accuracy:
        failures.append(
            f"selection accuracy {report.selection_accuracy:.0%} "
            f"< required {args.min_selection_accuracy:.0%}"
        )
    if report.mean_field_recall < args.min_field_recall:
        failures.append(
            f"mean patient-field recall {report.mean_field_recall:.0%} "
            f"< required {args.min_field_recall:.0%}"
        )
    if report.errors:
        failures.append(f"{len(report.errors)} transcript(s) raised: "
                        + ", ".join(s.name for s in report.errors))
    if failures:
        print("\nFAIL: " + "; ".join(failures))
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
