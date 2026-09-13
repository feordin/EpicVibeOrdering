"""CORS for browser-based CDS Hooks clients.

The reference CDS Hooks Sandbox (demo/reference-sandbox, http://localhost:8095)
is a browser SPA that POSTs to our service cross-origin with an `Authorization`
bearer header, which makes every call a preflighted CORS request.
"""

from fastapi.testclient import TestClient

from epicvibe.cds.app import create_app
from epicvibe.config import Settings

SANDBOX = "http://localhost:8095"


def _client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(**overrides)))


def test_default_allowed_origins_cover_the_browser_clients():
    origins = Settings().cors_allow_origins
    assert "http://localhost:8095" in origins   # CDS Hooks Sandbox
    assert "http://localhost:8090" in origins   # SMART App Launcher v2
    assert "http://localhost:8100" in origins   # in-repo mock EHR


def test_discovery_get_is_allowed_from_the_sandbox_origin():
    resp = _client().get("/cds-services", headers={"Origin": SANDBOX})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == SANDBOX


def test_preflight_for_a_hook_post_allows_authorization_header():
    resp = _client().options(
        "/cds-services/epicvibe-patient-view",
        headers={
            "Origin": SANDBOX,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == SANDBOX
    allow_headers = resp.headers["access-control-allow-headers"].lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_preflight_for_the_feedback_endpoint():
    # The sandbox POSTs accepted/overridden suggestions to <service>/feedback.
    resp = _client().options(
        "/cds-services/epicvibe-order-select/feedback",
        headers={
            "Origin": SANDBOX,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == SANDBOX


def test_unlisted_origin_gets_no_allow_origin_header():
    resp = _client().get("/cds-services", headers={"Origin": "http://evil.example"})
    # The request itself still succeeds server-side; the browser is what blocks
    # it, and it does so because the ACAO header is absent.
    assert "access-control-allow-origin" not in resp.headers


def test_cors_can_be_disabled_by_emptying_the_setting():
    resp = _client(cors_allow_origins=[]).get("/cds-services",
                                              headers={"Origin": SANDBOX})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
