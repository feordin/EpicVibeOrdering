"""CLI: replay Epic-shaped CDS Hooks requests against a running service and
validate the responses against Epic's documented rules.

    python -m epicvibe.replay [--url http://localhost:8000]
                              [--hook all|patient-view|order-select|order-sign]
                              [--warm-seconds 2.0]
"""

import argparse
import asyncio
import sys

import httpx

from epicvibe.replay.epic_fixtures import (epic_order_select_request,
                                           epic_order_sign_request,
                                           epic_patient_view_request)
from epicvibe.replay.validator import validate_epic_response

HOOK_ENDPOINTS = {
    "patient-view": "/cds-services/epicvibe-patient-view",
    "order-select": "/cds-services/epicvibe-order-select",
    "order-sign": "/cds-services/epicvibe-order-sign",
}

HOOK_REQUEST_BUILDERS = {
    "patient-view": epic_patient_view_request,
    "order-select": epic_order_select_request,
    "order-sign": epic_order_sign_request,
}

HOOK_ORDER = ["patient-view", "order-select", "order-sign"]


def _summarize(response: dict) -> tuple[int, int]:
    cards = response.get("cards") or []
    n_cards = len(cards)
    m_suggestions = sum(len(c.get("suggestions") or []) for c in cards)
    return n_cards, m_suggestions


async def _replay_hook(client: httpx.AsyncClient, hook: str) -> bool:
    request_body = HOOK_REQUEST_BUILDERS[hook]()
    resp = await client.post(HOOK_ENDPOINTS[hook], json=request_body)
    try:
        body = resp.json()
    except ValueError:
        print(f"{hook}: FAILED - non-JSON response (status {resp.status_code})")
        return False

    violations = validate_epic_response(body, hook)
    if violations:
        print(f"{hook}: VIOLATIONS")
        for v in violations:
            print(f"  - {v}")
        return False

    n_cards, m_suggestions = _summarize(body)
    print(f"{hook}: OK ({n_cards} cards, {m_suggestions} suggestions)")
    return True


async def _run(url: str, hook: str, warm_seconds: float) -> bool:
    hooks = HOOK_ORDER if hook == "all" else [hook]
    all_ok = True
    async with httpx.AsyncClient(base_url=url) as client:
        for h in hooks:
            ok = await _replay_hook(client, h)
            all_ok = all_ok and ok
            if h == "patient-view" and warm_seconds > 0:
                await asyncio.sleep(warm_seconds)
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m epicvibe.replay")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--hook", choices=["all", *HOOK_ORDER], default="all")
    parser.add_argument("--warm-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    ok = asyncio.run(_run(args.url, args.hook, args.warm_seconds))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
