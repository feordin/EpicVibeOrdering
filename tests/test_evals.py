import asyncio
import sys
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
    assert s.grounded and s.recall == 1.0 and s.precision == 1.0 and s.forbidden_hits == []

def test_partial_recall():
    s = score(_vp([_item("ITEM_A1C", "V_A1C")]), EXPECTED, "t")
    assert s.recall == 0.5 and s.precision == 1.0

def test_unexpected_item_hurts_precision_even_when_not_forbidden():
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_METFORMIN", "V_MET_500"),
                   _item("ITEM_RETINAL", "V_RETINAL")]), EXPECTED, "t")
    assert s.recall == 1.0 and s.precision == 2 / 3
    assert s.forbidden_hits == []

def test_forbidden_hurts_precision():
    exp = Expected(order_set_id="AMB_DM2_NEWDX", required_items=["ITEM_A1C"],
                   forbidden_items=["ITEM_RETINAL"])
    s = score(_vp([_item("ITEM_A1C", "V_A1C"), _item("ITEM_RETINAL", "V_RETINAL")]), exp, "t")
    assert s.precision == 0.5
    assert s.forbidden_hits == ["ITEM_RETINAL"]          # separate hard gate

def test_ungrounded_flagged():
    s = score(_vp([_item("ITEM_FAKE", "V_FAKE")]), EXPECTED, "t")
    assert s.grounded is False

def test_zero_scenarios_fails(tmp_path, monkeypatch):
    from evals import run as run_mod
    monkeypatch.setattr(sys, "argv", ["evals.run", "--scenarios", str(tmp_path)])
    assert asyncio.run(run_mod.main()) == 1

def test_eval_provider_defaults_to_demo(monkeypatch):
    from evals import run as run_mod
    monkeypatch.delenv("EPICVIBE_INFERENCE_PROVIDER", raising=False)
    assert run_mod.eval_settings().inference_provider == "demo"
    monkeypatch.setenv("EPICVIBE_INFERENCE_PROVIDER", "fake")
    assert run_mod.eval_settings().inference_provider == "fake"   # env still wins
    assert run_mod.eval_settings("demo").inference_provider == "demo"

def test_run_passes_with_the_demo_provider(monkeypatch, capsys):
    from evals import run as run_mod
    monkeypatch.setattr(sys, "argv", ["evals.run", "--provider", "demo"])
    assert asyncio.run(run_mod.main()) == 0
    out = capsys.readouterr().out
    assert "gate: mean recall >= 0.80" in out and "PASS" in out

def test_run_fails_the_gate_with_the_empty_fake_provider(monkeypatch, capsys):
    """The old harness reported recall 0.00 and still exited 0."""
    from evals import run as run_mod
    monkeypatch.setattr(sys, "argv", ["evals.run", "--provider", "fake"])
    assert asyncio.run(run_mod.main()) == 1
    out = capsys.readouterr().out
    assert "mean recall 0.00" in out and "FAIL" in out
