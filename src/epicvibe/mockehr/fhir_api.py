"""FHIR R4 REST surface for the mock EHR (read/search/create/update only)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

FHIR_JSON = "application/fhir+json"


def _fhir(payload: dict, status_code: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, media_type=FHIR_JSON,
                        headers=headers or {})


def _outcome(severity: str, code: str, text: str, status_code: int) -> JSONResponse:
    return _fhir({"resourceType": "OperationOutcome",
                  "issue": [{"severity": severity, "code": code,
                             "diagnostics": text, "details": {"text": text}}]},
                 status_code=status_code)


def _settings(request: Request):
    return request.app.state.settings


def _unauthorized(text: str) -> JSONResponse:
    response = _outcome("error", "login", text, 401)
    response.headers["WWW-Authenticate"] = 'Bearer realm="mockehr"'
    return response


def _denied(request: Request) -> JSONResponse | None:
    """SMART: every FHIR interaction needs a bearer token this server minted.

    ``metadata`` and ``.well-known/smart-configuration`` stay open -- a client has to
    read them *before* it can obtain a token.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, raw = header.partition(" ")
    token = raw.strip()
    if scheme.lower() != "bearer" or not token:
        return _unauthorized("an OAuth2 bearer token is required for FHIR access")
    if request.app.state.oauth.token_info(token) is None:
        return _unauthorized("invalid or expired access token")
    return None


def smart_configuration(base_url: str) -> dict:
    base = base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
        "grant_types_supported": ["authorization_code"],
        "scopes_supported": ["openid", "fhirUser", "launch", "launch/patient",
                             "patient/*.read", "user/*.read",
                             "patient/ServiceRequest.write", "patient/MedicationRequest.write"],
        "response_types_supported": ["code"],
        "capabilities": ["launch-ehr", "launch-standalone", "client-public",
                         "context-ehr-patient", "context-ehr-encounter",
                         "permission-patient", "permission-user"],
        "code_challenge_methods_supported": ["S256"],
    }


def capability_statement(base_url: str) -> dict:
    base = base_url.rstrip("/")
    oauth_ext = {
        "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
        "extension": [
            {"url": "authorize", "valueUri": f"{base}/oauth/authorize"},
            {"url": "token", "valueUri": f"{base}/oauth/token"},
        ],
    }
    resources = ["Patient", "Encounter", "Condition", "MedicationRequest", "Observation",
                 "AllergyIntolerance", "ServiceRequest", "Practitioner", "Organization"]
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-01-01",
        "publisher": "EpicVibe mock EHR",
        "kind": "instance",
        "software": {"name": "epicvibe-mockehr", "version": "0.1.0"},
        "implementation": {"description": "EpicVibe mock EHR", "url": f"{base}/fhir"},
        "fhirVersion": "4.0.1",
        "format": ["json", "application/fhir+json"],
        "rest": [{
            "mode": "server",
            "security": {
                "extension": [oauth_ext],
                "service": [{"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                    "code": "SMART-on-FHIR", "display": "SMART-on-FHIR"}]}],
            },
            "resource": [{"type": rtype, "interaction": [
                {"code": "read"}, {"code": "search-type"},
                {"code": "create"}, {"code": "update"}]} for rtype in resources],
        }],
    }


@router.get("/fhir/metadata")
async def metadata(request: Request):
    return _fhir(capability_statement(_settings(request).base_url))


@router.get("/fhir/.well-known/smart-configuration")
@router.get("/.well-known/smart-configuration")
async def smart_config(request: Request):
    return JSONResponse(smart_configuration(_settings(request).base_url))


@router.get("/fhir/{rtype}/{rid}")
async def read_resource(rtype: str, rid: str, request: Request):
    denied = _denied(request)
    if denied is not None:
        return denied
    resource = request.app.state.store.get(rtype, rid)
    if resource is None:
        return _outcome("error", "not-found", f"{rtype}/{rid} not found", 404)
    return _fhir(resource)


@router.get("/fhir/{rtype}")
async def search_resources(rtype: str, request: Request):
    denied = _denied(request)
    if denied is not None:
        return denied
    settings = _settings(request)
    params = dict(request.query_params)
    bundle = request.app.state.store.search_bundle(rtype, params, base_url=settings.fhir_base)
    return _fhir(bundle)


@router.post("/fhir/{rtype}")
async def create_resource(rtype: str, request: Request):
    denied = _denied(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _outcome("error", "structure", "request body is not valid JSON", 400)
    if not isinstance(body, dict):
        return _outcome("error", "structure", "request body must be a JSON object", 400)
    if body.get("resourceType") and body["resourceType"] != rtype:
        return _outcome("error", "invalid",
                        f"resourceType {body['resourceType']} does not match {rtype}", 400)
    resource = request.app.state.store.create(rtype, body)
    location = f"{_settings(request).fhir_base}/{rtype}/{resource['id']}"
    return _fhir(resource, status_code=201, headers={"Location": location})


@router.put("/fhir/{rtype}/{rid}")
async def update_resource(rtype: str, rid: str, request: Request):
    denied = _denied(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _outcome("error", "structure", "request body is not valid JSON", 400)
    existed = request.app.state.store.get(rtype, rid) is not None
    resource = request.app.state.store.update(rtype, rid, body)
    return _fhir(resource, status_code=200 if existed else 201)
