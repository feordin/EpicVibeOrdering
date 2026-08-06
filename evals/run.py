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
