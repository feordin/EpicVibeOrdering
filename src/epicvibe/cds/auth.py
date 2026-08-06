import logging

import jwt
from fastapi import HTTPException, Request

log = logging.getLogger("epicvibe.cds")

_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _signing_key(token: str, jwks_url: str):
    client = _jwks_clients.setdefault(jwks_url, jwt.PyJWKClient(jwks_url))
    return client.get_signing_key_from_jwt(token).key


def verify_epic_jwt(token: str, jwks_url: str, audience: str) -> dict:
    key = _signing_key(token, jwks_url)
    return jwt.decode(token, key, algorithms=["RS256", "RS384", "ES256"], audience=audience,
                       options={"require": ["exp"]})


async def epic_auth(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.verify_jwt:
        return
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401)
    try:
        verify_epic_jwt(header[7:], settings.jwks_url, settings.jwt_audience)
    except HTTPException:
        raise
    except Exception:
        log.warning("JWT verification failed")
        raise HTTPException(status_code=401)
