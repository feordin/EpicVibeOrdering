import json
from pathlib import Path
import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from epicvibe.cds import auth as auth_mod
from epicvibe.cds.app import create_app
from epicvibe.config import Settings
from epicvibe.inference.base import FakeProvider

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

def _token(aud="epicvibe"):
    return pyjwt.encode({"iss": "epic", "aud": aud}, KEY, algorithm="RS256")

@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch):
    monkeypatch.setattr(auth_mod, "_signing_key", lambda token, jwks_url: KEY.public_key())

def _app():
    s = Settings(_env_file=None, audit_db_path=":memory:", verify_jwt=True,
                 jwks_url="https://example.org/jwks", jwt_audience="epicvibe")
    return create_app(s, provider=FakeProvider({"order_sets": [], "confidence": "low"}))

def _body():
    return json.loads(Path("fixtures/hooks/patient_view.json").read_text())

async def test_valid_token_accepted():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body(),
                         headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200

async def test_missing_token_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body())
    assert r.status_code == 401

async def test_wrong_audience_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body(),
                         headers={"Authorization": f"Bearer {_token(aud='other')}"})
    assert r.status_code == 401

async def test_disabled_by_default():
    s = Settings(_env_file=None, audit_db_path=":memory:")
    app = create_app(s, provider=FakeProvider({"order_sets": [], "confidence": "low"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/cds-services/epicvibe-patient-view", json=_body())
    assert r.status_code == 200
