"""In-memory SMART launch/session state.

Deliberately process-local and short-lived: it holds an access token and the
FHIR context for one browser tab.  Nothing here is persisted and nothing here
is logged.
"""

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

COOKIE_NAME = "epicvibe_smart"
SESSION_TTL_SECONDS = 60 * 60


def new_id() -> str:
    return secrets.token_urlsafe(24)


def code_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass
class LaunchState:
    state: str
    iss: str
    verifier: str
    launch: str = ""
    authorize_url: str = ""
    token_url: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class SmartSession:
    session_id: str
    iss: str
    access_token: str = ""
    token_type: str = "Bearer"
    patient: str = ""
    encounter: str = ""
    scope: str = ""
    created_at: float = field(default_factory=time.time)
    # The last ValidatedProposal rendered into this tab.  Kept so a hand-back
    # submit can diff the clinician's selections against what the AI proposed
    # (rationale, evidence, and the items they deselected).
    proposal: Any = None

    @property
    def auth_header(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"{self.token_type or 'Bearer'} {self.access_token}"}


class SessionStore:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._launches: dict[str, LaunchState] = {}
        self._sessions: dict[str, SmartSession] = {}

    # --- pending launches (keyed by oauth `state`) ---------------------------
    def put_launch(self, launch_state: LaunchState) -> None:
        self._sweep()
        self._launches[launch_state.state] = launch_state

    def pop_launch(self, state: str) -> LaunchState | None:
        return self._launches.pop(state, None)

    # --- authorized sessions (keyed by cookie value) -------------------------
    def put_session(self, session: SmartSession) -> None:
        self._sweep()
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str | None) -> SmartSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.created_at > self._ttl:
            self._sessions.pop(session_id, None)
            return None
        return session

    def _sweep(self) -> None:
        now = time.time()
        for key, value in list(self._launches.items()):
            if now - value.created_at > self._ttl:
                self._launches.pop(key, None)
        for key, value in list(self._sessions.items()):
            if now - value.created_at > self._ttl:
                self._sessions.pop(key, None)
