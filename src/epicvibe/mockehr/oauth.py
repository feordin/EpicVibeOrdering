"""SMART on FHIR EHR-launch stub.

No login screen, no consent screen, no real crypto: ``/oauth/authorize`` auto-approves
and redirects with a code, ``/oauth/token`` swaps that code for a bearer token carrying
the launch context. Enough for a SMART app to complete the EHR launch handshake locally.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()

TOKEN_TTL_SECONDS = 3600
DEFAULT_SCOPE = "launch openid fhirUser patient/*.read user/*.read"

#: The one client this stub has "registered". Its redirect URI must live under the
#: configured CDS base URL; anything else is only allowed to redirect to loopback.
REGISTERED_CLIENT_ID = "epicvibe-order-assistant"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class LaunchContext:
    """The EHR-side launch context a SMART app inherits."""

    launch_id: str
    patient_id: str
    encounter_id: str | None = None
    user_id: str = "Practitioner/pr-1"
    created_at: float = field(default_factory=time.time)

    def as_token_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"patient": self.patient_id, "need_patient_banner": False}
        if self.encounter_id:
            params["encounter"] = self.encounter_id
        return params


@dataclass
class AuthCode:
    code: str
    launch: LaunchContext
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str = ""
    code_challenge_method: str = "S256"
    created_at: float = field(default_factory=time.time)


@dataclass
class AccessToken:
    token: str
    launch: LaunchContext | None
    scope: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class OAuthStub:
    """In-memory launch contexts, auth codes and access tokens."""

    def __init__(self) -> None:
        self.launches: dict[str, LaunchContext] = {}
        self.codes: dict[str, AuthCode] = {}
        self.tokens: dict[str, AccessToken] = {}

    def reset(self) -> None:
        self.launches.clear()
        self.codes.clear()
        self.tokens.clear()

    def create_launch(self, patient_id: str, encounter_id: str | None = None,
                      user_id: str = "Practitioner/pr-1") -> LaunchContext:
        launch_id = f"launch-{secrets.token_hex(6)}"
        ctx = LaunchContext(launch_id=launch_id, patient_id=patient_id,
                            encounter_id=encounter_id, user_id=user_id)
        self.launches[launch_id] = ctx
        return ctx

    def mint_token(self, launch: LaunchContext | None = None,
                   scope: str = "patient/*.read user/*.read") -> str:
        token = f"mockehr-{secrets.token_urlsafe(24)}"
        self.tokens[token] = AccessToken(token=token, launch=launch, scope=scope,
                                         expires_at=time.time() + TOKEN_TTL_SECONDS)
        return token

    def token_info(self, token: str) -> AccessToken | None:
        info = self.tokens.get(token)
        return None if info is None or info.expired else info


def _stub(request: Request) -> OAuthStub:
    return request.app.state.oauth


def validate_redirect_uri(redirect_uri: str, client_id: str, cds_base_url: str) -> None:
    """Reject open redirects. Raises ``HTTPException(400)``.

    An auth code is handed to whatever this URI points at, so it is the one thing in
    this stub that is not allowed to be permissive.
    """
    parsed = urlsplit(redirect_uri)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400,
                            detail="redirect_uri must be an http(s) URL")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="redirect_uri has no host")
    if client_id == REGISTERED_CLIENT_ID:
        base = (cds_base_url or "").rstrip("/")
        if not base or not (redirect_uri == base or redirect_uri.startswith(base + "/")):
            raise HTTPException(
                status_code=400,
                detail=f"redirect_uri for {REGISTERED_CLIENT_ID} must start with {base}")
        return
    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=400,
                            detail="redirect_uri must point at a loopback host")


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_pkce(auth_code: AuthCode, code_verifier: str | None) -> str | None:
    """Return an error description, or None when PKCE is satisfied.

    PKCE stays optional (the demo SMART app may omit it), but a `code_challenge` that
    *was* supplied is always enforced.
    """
    if not auth_code.code_challenge:
        return None
    if not code_verifier:
        return "code_verifier is required because the request used PKCE"
    method = (auth_code.code_challenge_method or "S256").upper()
    if method == "PLAIN":
        actual = code_verifier
    elif method == "S256":
        actual = _s256(code_verifier)
    else:
        return f"unsupported code_challenge_method {auth_code.code_challenge_method!r}"
    return None if secrets.compare_digest(actual, auth_code.code_challenge) else         "code_verifier does not match code_challenge"


@router.get("/oauth/authorize")
async def authorize(request: Request):
    """Auto-approve an authorization request and redirect back with a code."""
    q = request.query_params
    redirect_uri = q.get("redirect_uri")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri is required")
    if q.get("response_type", "code") != "code":
        raise HTTPException(status_code=400, detail="only response_type=code is supported")
    client_id = q.get("client_id", "unknown")
    validate_redirect_uri(redirect_uri, client_id,
                          getattr(request.app.state.settings, "cds_base_url", ""))

    stub = _stub(request)
    launch_id = q.get("launch")
    launch = stub.launches.get(launch_id) if launch_id else None
    if launch is None and launch_id and launch_id.startswith("dev-"):
        # A dev-launch id (see smart/routes.py's dev_launch): "dev-<patientId>". Resolve
        # it to that specific patient rather than falling back to the first patient.
        store = request.app.state.store
        patient_id = launch_id[len("dev-"):]
        if store.get("Patient", patient_id) is not None:
            encounters = store.search("Encounter", {"patient": patient_id})
            in_progress = [e for e in encounters if e.get("status") == "in-progress"]
            pool = in_progress or encounters
            encounter = pool[0] if pool else None
            launch = stub.create_launch(patient_id, encounter["id"] if encounter else None)
    if launch is None:
        # Standalone launch (or an unknown launch id): fall back to the first patient.
        patients = request.app.state.store.all_of("Patient")
        if not patients:
            raise HTTPException(status_code=400, detail="no patients in store")
        launch = stub.create_launch(patients[0]["id"])

    code = f"code-{secrets.token_urlsafe(16)}"
    stub.codes[code] = AuthCode(code=code, launch=launch,
                                client_id=client_id,
                                redirect_uri=redirect_uri,
                                scope=q.get("scope", DEFAULT_SCOPE),
                                code_challenge=q.get("code_challenge", ""),
                                code_challenge_method=q.get("code_challenge_method", "S256"))
    params = {"code": code}
    if q.get("state"):
        params["state"] = q["state"]
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def _form(request: Request) -> dict[str, str]:
    """Parse an ``application/x-www-form-urlencoded`` body (JSON tolerated too).

    Deliberately hand-rolled: FastAPI's ``Form(...)`` requires python-multipart, which
    is not a dependency of this project.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            parsed = json.loads(raw or "{}")
            return {k: str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {k: v for k, v in parse_qsl(raw, keep_blank_values=True)}


@router.post("/oauth/token")
async def token(request: Request):
    form = await _form(request)
    grant_type = form.get("grant_type", "authorization_code")
    code = form.get("code")
    stub = _stub(request)
    if grant_type not in ("authorization_code", "refresh_token"):
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if grant_type == "refresh_token":
        return JSONResponse({"error": "unsupported_grant_type",
                             "error_description": "mock EHR does not issue refresh tokens"},
                            status_code=400)
    auth_code = stub.codes.pop(code, None) if code else None
    if auth_code is None:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "unknown or already-redeemed code"},
                            status_code=400)

    redirect_uri = form.get("redirect_uri")
    if redirect_uri is not None and redirect_uri != auth_code.redirect_uri:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "redirect_uri does not match the "
                                                  "one used for the authorization request"},
                            status_code=400)
    client_id = form.get("client_id")
    if client_id is not None and client_id != auth_code.client_id:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "client_id does not match the "
                                                  "authorization code"},
                            status_code=400)
    pkce_error = verify_pkce(auth_code, form.get("code_verifier"))
    if pkce_error:
        return JSONResponse({"error": "invalid_grant", "error_description": pkce_error},
                            status_code=400)

    access_token = stub.mint_token(auth_code.launch, scope=auth_code.scope)
    body: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "scope": auth_code.scope,
        **auth_code.launch.as_token_params(),
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
