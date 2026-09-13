import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from epicvibe.catalog.index import CatalogIndex
from epicvibe.catalog.loader import load_catalog
from epicvibe.config import Settings
from epicvibe.inference.factory import make_provider
from epicvibe.proposal.engine import ProposalEngine
from epicvibe.proposal.patient_summary import summarize_prefetch
from evals.scoring import Expected, score

# Gates: the harness must fail loudly rather than report a green 0.00 recall.
MIN_MEAN_RECALL = 0.8
MIN_SCENARIO_RECALL = 0.5
# The `fake` provider returns an empty proposal, so it scores 0 by construction;
# evals default to `demo` unless the environment says otherwise.
DEFAULT_EVAL_PROVIDER = "demo"


def eval_settings(provider: str | None = None) -> Settings:
    settings = Settings()
    chosen = provider or os.environ.get("EPICVIBE_INFERENCE_PROVIDER") or DEFAULT_EVAL_PROVIDER
    return settings.model_copy(update={"inference_provider": chosen})


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=Path, default=Path("evals/scenarios"))
    ap.add_argument("--catalog", type=Path, default=Path("fixtures/catalog/sample_catalog.json"))
    ap.add_argument("--provider", default=None,
                    help="override the inference provider (default: demo, or "
                         "EPICVIBE_INFERENCE_PROVIDER if set)")
    args = ap.parse_args()

    scenario_paths = sorted(args.scenarios.glob("*.json"))
    if not scenario_paths:
        print(f"no scenarios found in {args.scenarios}")
        return 1

    settings = eval_settings(args.provider)
    print(f"provider: {settings.inference_provider}")
    engine = ProposalEngine(CatalogIndex(load_catalog(args.catalog)), make_provider(settings))
    scores = []
    for path in scenario_paths:
        sc = json.loads(path.read_text())
        summary = summarize_prefetch(sc["context"], sc["prefetch"])
        vp = await engine.generate(summary)
        s = score(vp, Expected.model_validate(sc["expected"]), sc["name"])
        scores.append(s)
        line = (f"{s.name:30s} grounded={s.grounded} "
                f"recall={s.recall:.2f} precision={s.precision:.2f}")
        if s.forbidden_hits:
            line += f" FORBIDDEN={','.join(s.forbidden_hits)}"
        print(line)

    mean_recall = sum(s.recall for s in scores) / len(scores)
    print(f"\n{len(scores)} scenarios | grounding "
          f"{sum(s.grounded for s in scores)}/{len(scores)} | "
          f"mean recall {mean_recall:.2f} | "
          f"mean precision {sum(s.precision for s in scores)/len(scores):.2f}")

    failures = []
    if not all(s.grounded for s in scores):
        failures.append("grounding: every scenario must be violation-free")
    if mean_recall < MIN_MEAN_RECALL:
        failures.append(f"mean recall {mean_recall:.2f} < {MIN_MEAN_RECALL:.2f}")
    for s in scores:
        if s.recall < MIN_SCENARIO_RECALL:
            failures.append(f"{s.name}: recall {s.recall:.2f} < {MIN_SCENARIO_RECALL:.2f}")
        if s.forbidden_hits:
            failures.append(f"{s.name}: forbidden items included "
                            f"({', '.join(s.forbidden_hits)})")

    print(f"gate: mean recall >= {MIN_MEAN_RECALL:.2f}, per-scenario recall >= "
          f"{MIN_SCENARIO_RECALL:.2f}, grounded, no forbidden items")
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
