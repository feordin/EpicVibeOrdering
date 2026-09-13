#!/usr/bin/env python3
"""Drive the reference CDS Hooks Sandbox with Playwright and screenshot the result.

Prerequisites (see docs/runbook-reference-sandbox.md):
  * `docker compose up -d --build` in this directory (HAPI :8080, launcher :8090,
    sandbox :8095) and the one-shot `seed` container finished successfully;
  * the EpicVibe CDS service running on the HOST at :8000 with
    EPICVIBE_INFERENCE_PROVIDER=demo.

Run:  .venv/Scripts/python demo/reference-sandbox/drive_sandbox.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
SHOTS = HERE / "screenshots"
SANDBOX = "http://localhost:8095"
FHIR = "http://localhost:8090/v/r4/fhir"      # SMART App Launcher R4 proxy -> HAPI
DISCOVERY = "http://localhost:8000/cds-services"
PATIENT = "pat-okafor"

URL = (f"{SANDBOX}/?fhirServiceUrl={urllib.parse.quote(FHIR, safe='')}"
       f"&serviceDiscoveryURL={urllib.parse.quote(DISCOVERY, safe='')}"
       f"&patientId={PATIENT}")

notes: list[str] = []
console: list[str] = []


def note(msg: str) -> None:
    print(msg, flush=True)
    notes.append(msg)


def cards_text(page) -> str:
    """All text in the left-hand (mock-EHR) pane, where CardList renders."""
    return page.inner_text("body")


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:400]))
        requests: list[str] = []
        page.on("request", lambda r: requests.append(f"{r.method} {r.url}")
                if ":8000" in r.url else None)

        # ---- 1. load with FHIR server + discovery URL + patient preconfigured ----
        note(f"opening {URL}")
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(4000)
        body = cards_text(page)
        note(f"patient banner contains 'Okafor': {'Okafor' in body}")
        note(f"discovery calls to :8000 so far: "
             f"{sum(1 for r in requests if r.endswith('/cds-services'))}")

        # ---- 2. patient-view card (poll: first hook call only enqueues) ----
        got_patient_card = False
        for attempt in range(1, 5):
            page.wait_for_timeout(3000)
            # Re-fire patient-view by bouncing through Rx View and back; the
            # sandbox only re-invokes CDS when the hook context changes.
            page.click("text=Rx View")
            page.wait_for_timeout(2500)
            page.click("text=Patient View")
            page.wait_for_timeout(3500)
            body = cards_text(page)
            if "EpicVibe Ordering" in body:
                got_patient_card = True
                note(f"patient-view card rendered on poll #{attempt}")
                break
            note(f"poll #{attempt}: no EpicVibe card yet")
        note(f"PATIENT-VIEW CARD: {'OK' if got_patient_card else 'NOT RENDERED'}")
        page.screenshot(path=str(SHOTS / "01-patient-view-card.png"))

        # ---- 3. order-select on the Rx view ----
        page.click("text=Rx View")
        page.wait_for_timeout(2000)
        # Type a medication and pick it from the static list to populate
        # draftOrders; order-select does not fire until a med is chosen.
        try:
            box = page.locator("input[name='medication-input']")
            box.click()
            box.fill("amoxicillin")
            # The match list renders as MUI ListItemButtons under the field.
            page.wait_for_selector("form li[role='button'], form .MuiListItemButton-root",
                                   timeout=15_000)
            options = page.locator("form .MuiListItemButton-root")
            note(f"medication matches offered: {options.count()}")
            options.first.click()
        except Exception as exc:  # noqa: BLE001
            note(f"medication input interaction failed: {exc}")
        page.wait_for_timeout(4000)
        body = cards_text(page)
        got_order_card = "EpicVibe Ordering" in body
        note(f"ORDER-SELECT CARD: {'OK' if got_order_card else 'NOT RENDERED'}")

        # Suggestion buttons are MUI contained buttons inside the card.
        suggestion_labels: list[str] = []
        buttons = page.locator("button")
        for i in range(buttons.count()):
            t = (buttons.nth(i).inner_text() or "").strip()
            if t and t not in {"Patient View", "Rx View", "Rx Sign", "PAMA Imaging",
                               "Dismiss", ""}:
                suggestion_labels.append(t)
        note(f"buttons on the order-select screen: {suggestion_labels[:20]}")
        page.screenshot(path=str(SHOTS / "02-order-select-suggestions.png"))

        # ---- 4. Accept a suggestion ----
        # Two cases matter: a ServiceRequest `create` (lab/imaging) and a
        # MedicationRequest `create`.  The sandbox only does anything with the
        # latter, so try one of each and record what changed.
        def med_field() -> str:
            try:
                return page.input_value("input[name='medication-input']")
            except Exception:  # noqa: BLE001
                return "<unreadable>"

        def order_select_calls() -> int:
            return sum(1 for r in requests
                       if r.endswith("/cds-services/epicvibe-order-select"))

        def accept(label: str) -> None:
            before = order_select_calls()
            try:
                page.click(f'button:text-is("{label}")', timeout=5000)
                page.wait_for_timeout(4000)
                note(f"accepted {label!r} -> medication field {med_field()!r}, "
                     f"order-select re-fired {order_select_calls() - before}x")
            except Exception as exc:  # noqa: BLE001
                note(f"could not accept {label!r}: {exc}")

        note(f"medication field before accept: {med_field()!r}")
        service_request_labels = [x for x in suggestion_labels
                                  if x.startswith(("CBC", "Basic Metabolic"))]
        medication_labels = [x for x in suggestion_labels
                             if "mg" in x.lower() or " g " in x.lower()]
        if service_request_labels:
            accept(service_request_labels[0])
        if medication_labels:
            accept(medication_labels[0])
        feedback_calls = [r for r in requests if r.endswith("/feedback")]
        note(f"feedback POSTs to our service: {len(feedback_calls)} "
             f"({sorted(set(feedback_calls))})")

        # ---- 5. SMART link ----
        smart_disabled = page.locator("text=Cannot launch SMART link").count()
        note(f"'Cannot launch SMART link without a SMART-enabled FHIR server' "
             f"notices on screen: {smart_disabled}")
        page.screenshot(path=str(SHOTS / "03-after-accept.png"))

        browser.close()

    (SHOTS / "..").resolve()
    print("\n===== CONSOLE (last 40) =====")
    for line in console[-40:]:
        print(line)
    print("\n===== NOTES =====")
    print(json.dumps(notes, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
