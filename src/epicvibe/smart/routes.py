"""SMART on FHIR EHR-launch flow plus the order-assistant API.

Flow:  /smart/launch -> EHR authorize -> /smart/callback -> /smart/app
The app then calls /smart/api/context, /smart/api/proposal and /smart/api/submit.
"""

import logging
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from epicvibe.cache import refinement_cache
from epicvibe.cds.cards import build_order_resource
from epicvibe.inference.base import describe_provider
from epicvibe.proposal.patient_summary import summarize_resources
from epicvibe.proposal.refinement import build_refinement
from epicvibe.smart.fhir import create_resource, fetch_patient_data, read_json
from epicvibe.smart.page import APP_HTML
from epicvibe.smart.session import (COOKIE_NAME, LaunchState, SessionStore, SmartSession,
                                    code_challenge, code_verifier, new_id)
from epicvibe.smart.template import patient_banner, render_template

log = logging.getLogger("epicvibe.smart")
router = APIRouter(prefix="/smart", tags=["smart"])

DISCOVERY_TIMEOUT = 5.0
TOKEN_TIMEOUT = 10.0


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def _store(request: Request) -> SessionStore:
    state = request.app.state
    if not hasattr(state, "smart_sessions"):
        state.smart_sessions = SessionStore()
    return state.smart_sessions


def _client(request: Request) -> httpx.AsyncClient:
    """Shared outbound client; tests inject one that targets a fake EHR."""
    state = request.app.state
    client = getattr(state, "http_client", None)
    if client is None:
        # Never follow redirects: `iss` is only validated against the allowlist
        # before the fetch, so a redirect would hop straight past that check.
        client = httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, follow_redirects=False)
        state.http_client = client
    return client


def _normalize_issuer(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return None
    return (parts.scheme.lower(), parts.hostname.lower(), parts.port)


def check_issuer_allowed(iss: str, allowed: list[str]) -> str:
    """Return the normalized `iss`, or 400 if it is not on the allowlist."""
    normalized = _normalize_issuer(iss)
    if not normalized or normalized not in {_normalize_issuer(a) for a in allowed or []}:
        log.info("smart launch rejected: issuer not allowed")
        raise HTTPException(status_code=400, detail="iss is not an allowed SMART issuer")
    return normalized


def check_endpoint_origin(iss: str, *endpoints: str) -> None:
    """Discovered OAuth endpoints must live on the same origin as `iss`."""
    expected = _origin(iss)
    for endpoint in endpoints:
        if not endpoint:
            continue
        if _origin(endpoint) != expected:
            log.info("smart launch rejected: discovered endpoint origin mismatch")
            raise HTTPException(
                status_code=400,
                detail="discovered SMART endpoint is not on the issuer's origin")


async def discover_endpoints(client: httpx.AsyncClient, iss: str) -> tuple[str, str]:
    """(authorize_url, token_url) from smart-configuration, else CapabilityStatement."""
    config = await read_json(client, iss, ".well-known/smart-configuration", {},
                             timeout=DISCOVERY_TIMEOUT)
    if isinstance(config, dict) and config.get("authorization_endpoint"):
        return config["authorization_endpoint"], config.get("token_endpoint", "")

    metadata = await read_json(client, iss, "metadata", {}, timeout=DISCOVERY_TIMEOUT)
    authorize = token = ""
    for rest in (metadata or {}).get("rest") or []:
        for ext in (rest.get("security") or {}).get("extension") or []:
            if not str(ext.get("url", "")).endswith("oauth-uris"):
                continue
            for sub in ext.get("extension") or []:
                if sub.get("url") == "authorize":
                    authorize = sub.get("valueUri") or ""
                elif sub.get("url") == "token":
                    token = sub.get("valueUri") or ""
    if not authorize:
        raise HTTPException(status_code=502, detail="SMART authorization endpoint not discoverable")
    return authorize, token


def _session(request: Request) -> SmartSession:
    session = _store(request).get_session(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=401, detail="SMART session expired; relaunch the app")
    return session


# --------------------------------------------------------------------------- #
# launch / callback
# --------------------------------------------------------------------------- #

async def _begin_launch(request: Request, iss: str, launch: str) -> RedirectResponse:
    settings = request.app.state.settings
    iss = check_issuer_allowed(iss, settings.smart_allowed_issuers)
    client = _client(request)
    authorize_url, token_url = await discover_endpoints(client, iss)
    check_endpoint_origin(iss, authorize_url, token_url)

    verifier = code_verifier()
    launch_state = LaunchState(state=new_id(), iss=iss, verifier=verifier, launch=launch,
                               authorize_url=authorize_url, token_url=token_url)
    _store(request).put_launch(launch_state)

    params = {
        "response_type": "code",
        "client_id": settings.smart_client_id,
        "redirect_uri": settings.smart_redirect_uri,
        "scope": settings.smart_scope,
        "state": launch_state.state,
        "aud": iss,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if launch:
        params["launch"] = launch
    return RedirectResponse(f"{authorize_url}?{urlencode(params)}", status_code=302)


@router.get("/launch")
async def launch(request: Request, iss: str, launch: str = "") -> RedirectResponse:
    return await _begin_launch(request, iss, launch)


@router.get("/dev/launch")
async def dev_launch(request: Request, patient: str = "pat-santos",
                     encounter: str = "") -> RedirectResponse:
    """Standalone dev entry point against the local mock EHR."""
    settings = request.app.state.settings
    if not settings.smart_dev_mode:
        raise HTTPException(status_code=404, detail="dev mode disabled")
    return await _begin_launch(request, settings.smart_dev_iss, f"dev-{patient}")


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    settings = request.app.state.settings
    store = _store(request)
    launch_state = store.pop_launch(state)
    if launch_state is None or not code:
        raise HTTPException(status_code=400, detail="unknown or expired SMART launch state")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.smart_redirect_uri,
        "client_id": settings.smart_client_id,
        "code_verifier": launch_state.verifier,
    }
    token_url = launch_state.token_url or f"{launch_state.iss.rstrip('/')}/token"
    try:
        resp = await _client(request).post(
            token_url, data=form, timeout=TOKEN_TIMEOUT,
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
    except Exception:
        log.info("smart token exchange transport error")
        raise HTTPException(status_code=502, detail="token exchange failed")
    if resp.status_code >= 400:
        log.info("smart token exchange non-2xx: %s", resp.status_code)
        raise HTTPException(status_code=502, detail="token exchange rejected by the EHR")
    try:
        token = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="token response was not JSON")

    session = SmartSession(
        session_id=new_id(), iss=launch_state.iss,
        access_token=token.get("access_token", ""),
        token_type=token.get("token_type", "Bearer") or "Bearer",
        patient=str(token.get("patient") or ""),
        encounter=str(token.get("encounter") or ""),
        scope=token.get("scope", ""))
    store.put_session(session)

    response = RedirectResponse("/smart/app", status_code=302)
    response.set_cookie(COOKIE_NAME, session.session_id, httponly=True,
                        samesite="lax", path="/smart")
    return response


# --------------------------------------------------------------------------- #
# app + API
# --------------------------------------------------------------------------- #

@router.get("/app", response_class=HTMLResponse)
async def app_page() -> HTMLResponse:
    return HTMLResponse(APP_HTML)


@router.get("/api/context")
async def api_context(request: Request) -> dict:
    session = _session(request)
    return {"iss": session.iss, "patient": session.patient,
            "encounter": session.encounter, "scope": session.scope,
            "authorized": bool(session.access_token),
            "submit_mode": request.app.state.settings.smart_submit_mode}


@router.post("/api/proposal")
async def api_proposal(request: Request) -> JSONResponse:
    session = _session(request)
    state = request.app.state
    if not session.patient:
        raise HTTPException(status_code=400, detail="launch context has no patient")

    resources = await fetch_patient_data(_client(request), session.iss, session.auth_header,
                                         session.patient, session.encounter)
    summary = summarize_resources(session.patient, session.encounter or None, resources)
    vp = await state.engine.generate(summary)
    session.proposal = vp
    try:
        state.audit.record_proposal(f"smart:{session.encounter or session.patient}", vp,
                                    model=describe_provider(state.engine.provider))
    except Exception:
        log.info("audit write for SMART proposal failed")

    patients = resources.get("Patient", [])
    return JSONResponse({
        "banner": patient_banner(patients[0] if patients else None),
        "counts": summary.data_counts(),
        "confidence": vp.proposal.confidence,
        "violations": [v.model_dump() for v in vp.violations],
        "notes": summary.notes,
        "order_sets": render_template(vp, state.index),
    })


@router.post("/api/submit")
async def api_submit(request: Request) -> JSONResponse:
    session = _session(request)
    state = request.app.state
    body = await request.json()
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="'items' must be a list")

    if state.settings.smart_submit_mode == "handback":
        return _submit_handback(state, session, items)
    return await _submit_fhir(request, state, session, items)


def _submit_handback(state, session: SmartSession, items: list) -> JSONResponse:
    """Store the clinician's refined selections for the next CDS hook.

    Inside Epic this is the only way a SMART app can put orders in front of the
    clinician: the next order-select / order-sign for this encounter renders
    them as `create` suggestions, and accepting one files an unsigned order.
    """
    refined, failed = build_refinement(
        state.index, items, original=session.proposal,
        patient_id=session.patient, encounter_id=session.encounter)
    keys = refinement_cache(state).put_for(session.encounter, session.patient, refined)
    try:
        state.audit.record_refinement(
            keys[0] if keys else f"smart:{session.patient}", refined.refined)
    except Exception:
        log.info("audit write for SMART refinement failed")

    stored = refined.selected_count
    return JSONResponse({
        "mode": "handback", "stored": stored,
        "encounter": session.encounter or "", "patient": session.patient,
        "failed": failed,
        "message": ("Selections will appear as suggestions when you return to "
                    "order entry."),
    })


async def _submit_fhir(request: Request, state, session: SmartSession,
                       items: list) -> JSONResponse:
    """Direct FHIR create.  Works against the mock EHR / HAPI, not against Epic
    (see `docs/spikes/fhir-order-writeback.md`)."""
    created, failed = [], []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        found = state.index.get_item(entry.get("item_id", ""))
        if found is None:
            failed.append({"item_id": entry.get("item_id", ""), "error": "not in catalog"})
            continue
        _, item = found
        variant = state.index.get_variant(item.item_id, entry.get("variant_id", ""))
        if variant is None:
            failed.append({"item_id": item.item_id, "error": "variant not in catalog"})
            continue
        fields = entry.get("fields") or {}
        resource = build_order_resource(
            item, variant, session.patient, None,
            encounter_id=session.encounter or None,
            status="draft", intent="order",
            overrides={k: str(v) for k, v in fields.items()} if isinstance(fields, dict) else None)
        ok, detail = await create_resource(_client(request), session.iss,
                                           session.auth_header, resource)
        if ok:
            created.append({"item_id": item.item_id, "label": variant.display,
                            "resource_type": resource["resourceType"], "id": detail,
                            "url": f"{session.iss.rstrip('/')}/{resource['resourceType']}/{detail}"})
        else:
            failed.append({"item_id": item.item_id, "error": detail})

    try:
        state.audit.record_feedback("epicvibe-smart-app", {"feedback": [
            {"card": c["id"], "outcome": "accepted"} for c in created]})
    except Exception:
        log.info("audit write for SMART submit failed")

    return JSONResponse({"mode": "fhir", "created": created, "failed": failed})
