#!/usr/bin/env python3
"""Seed the reference-sandbox HAPI FHIR server from the in-repo mock-EHR fixtures.

The fixtures in fixtures/mockehr/patients/*.json are FHIR *collection* Bundles.
A collection bundle cannot be POSTed to a FHIR server, and POSTing the entries
individually would let the server assign new ids -- but the whole demo (and the
CDS Hooks sandbox URL, and docs) depends on the literal ids `pat-okafor`,
`pat-santos`, `pat-brooks`, `pr-1`.  So we rewrite each collection into a
*transaction* bundle where every entry is a PUT to `<ResourceType>/<id>`, which
is an upsert that preserves the client-supplied id (HAPI needs
`hapi.fhir.client_id_strategy=ANY` for non-numeric ids; see docker-compose.yml).

Runs with the Python standard library only, so it needs no image build.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

FHIR_BASE = os.environ.get("FHIR_BASE", "http://hapi:8080/fhir").rstrip("/")
FIXTURE_DIR = pathlib.Path(os.environ.get("FIXTURE_DIR", "/fixtures"))
# Practitioners/Organizations first: patient resources reference them.
ORDER = ["practitioners.json", "santos.json", "okafor.json", "brooks.json"]
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "300"))


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/fhir+json")
    if data is not None:
        req.add_header("Content-Type", "application/fhir+json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_for_hapi() -> None:
    deadline = time.time() + WAIT_SECONDS
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            status, _ = _request("GET", f"{FHIR_BASE}/metadata")
            if status == 200:
                print(f"[seed] HAPI ready at {FHIR_BASE} (after {attempt} attempts)")
                return
            print(f"[seed] waiting for HAPI: HTTP {status}")
        except Exception as exc:  # noqa: BLE001 - connection refused etc.
            print(f"[seed] waiting for HAPI: {exc.__class__.__name__}: {exc}")
        time.sleep(3)
    raise SystemExit(f"[seed] HAPI at {FHIR_BASE} never became ready")


def to_transaction(bundle: dict) -> dict:
    """collection Bundle -> transaction Bundle of PUT-by-id upserts."""
    entries = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource")
        if not resource:
            continue
        rtype = resource.get("resourceType")
        rid = resource.get("id")
        if not rtype or not rid:
            print(f"[seed]   skipping entry without resourceType/id: {entry!r:.120}")
            continue
        entries.append({
            "fullUrl": f"{FHIR_BASE}/{rtype}/{rid}",
            "resource": resource,
            "request": {"method": "PUT", "url": f"{rtype}/{rid}"},
        })
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def load(path: pathlib.Path) -> bool:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    txn = to_transaction(bundle)
    status, body = _request("POST", f"{FHIR_BASE}", txn)
    if status not in (200, 201):
        print(f"[seed] FAILED {path.name}: HTTP {status}\n{body.decode(errors='replace')[:4000]}")
        return False
    try:
        response = json.loads(body)
    except ValueError:
        print(f"[seed] FAILED {path.name}: non-JSON response")
        return False
    bad = [
        e.get("response", {}).get("status", "?")
        for e in response.get("entry", [])
        if not str(e.get("response", {}).get("status", "")).startswith(("200", "201"))
    ]
    print(f"[seed] {path.name}: {len(txn['entry'])} resources upserted"
          + (f" ({len(bad)} non-2xx: {bad})" if bad else ""))
    return not bad


def verify() -> bool:
    ok = True
    for ref in ("Patient/pat-okafor", "Patient/pat-santos", "Patient/pat-brooks",
                "Practitioner/pr-1"):
        status, body = _request("GET", f"{FHIR_BASE}/{ref}")
        label = "OK " if status == 200 else "MISSING"
        print(f"[seed] verify {ref}: {label} (HTTP {status})")
        ok = ok and status == 200
    return ok


def main() -> int:
    wait_for_hapi()
    all_ok = True
    for name in ORDER:
        path = FIXTURE_DIR / name
        if not path.exists():
            print(f"[seed] fixture missing: {path}")
            all_ok = False
            continue
        all_ok = load(path) and all_ok
    all_ok = verify() and all_ok
    print("[seed] done" if all_ok else "[seed] done WITH ERRORS")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
