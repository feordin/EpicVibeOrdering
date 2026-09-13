"""CDS Hooks client: discovery, prefetch resolution, hook invocation, feedback."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from epicvibe.mockehr.store import FhirStore, MissingContextValue, resolve_template

log = logging.getLogger("epicvibe.mockehr.hooks")

MAX_HISTORY = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HooksClient:
    """Talks to the CDS service on behalf of the mock EHR."""

    def __init__(self, settings, store: FhirStore, oauth, *,
                 client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.store = store
        self.oauth = oauth
        self._client = client
        self.services: list[dict] = []
        self.discovery_error: str | None = "not yet fetched"
        self.discovery_at: str | None = None
        self.history: list[dict] = []

    # ------------------------------------------------------------------ client
    def _acquire(self) -> tuple[httpx.AsyncClient, bool]:
        """Return (client, owned). Injected clients are reused, not closed."""
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self.settings.hook_timeout_seconds), True

    def _url(self, path: str) -> str:
        if self._client is not None and self._client.base_url:
            return path
        return f"{self.settings.cds_base_url.rstrip('/')}{path}"

    # --------------------------------------------------------------- discovery
    async def refresh(self) -> dict:
        client, owned = self._acquire()
        try:
            response = await client.get(self._url("/cds-services"),
                                        timeout=self.settings.hook_timeout_seconds)
            response.raise_for_status()
            self.services = response.json().get("services", [])
            self.discovery_error = None
        except Exception as exc:  # the CDS service may simply not be running
            self.services = []
            self.discovery_error = f"{type(exc).__name__}: {exc}"
            log.warning("CDS discovery failed: %s", self.discovery_error)
        finally:
            self.discovery_at = _now_iso()
            if owned:
                await client.aclose()
        return self.discovery_status()

    def discovery_status(self) -> dict:
        return {"cdsBaseUrl": self.settings.cds_base_url,
                "ok": self.discovery_error is None,
                "error": self.discovery_error,
                "fetchedAt": self.discovery_at,
                "services": [{"id": s.get("id"), "hook": s.get("hook"),
                              "title": s.get("title"),
                              "prefetch": s.get("prefetch", {})} for s in self.services]}

    def service_for(self, hook: str) -> dict | None:
        for service in self.services:
            if service.get("hook") == hook:
                return service
        return None

    # ---------------------------------------------------------------- prefetch
    def resolve_prefetch(self, service: dict, context: dict) -> dict:
        """Execute each prefetch template in ``service`` against the local store."""
        prefetch: dict[str, Any] = {}
        for key, template in (service.get("prefetch") or {}).items():
            try:
                query = resolve_template(template, context)
            except MissingContextValue as exc:
                # Spec: a prefetch token with no value means the key is ABSENT from the
                # request. Running `Encounter/` would otherwise return every encounter
                # in the store -- a cross-patient leak.
                log.debug("prefetch %s omitted: no value for context.%s", key, exc.args[0])
                continue
            try:
                prefetch[key] = self.store.search_relative(
                    query, base_url=self.settings.fhir_base)
            except Exception:
                log.exception("prefetch %s failed for template %s", key, template)
        return prefetch

    # ------------------------------------------------------------------- hooks
    def build_request(self, hook: str, service: dict, *, patient_id: str,
                      encounter_id: str | None = None, user_id: str | None = None,
                      extra_context: dict | None = None) -> dict:
        context: dict[str, Any] = {
            "userId": user_id or self.settings.user_id,
            "patientId": patient_id,
        }
        if encounter_id:
            context["encounterId"] = encounter_id
        context.update(extra_context or {})
        body = {
            "hook": hook,
            "hookInstance": str(uuid4()),
            "fhirServer": self.settings.fhir_base,
            "fhirAuthorization": {
                "access_token": self.oauth.mint_token(),
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "patient/*.read user/*.read",
                "subject": "mockehr",
            },
            "context": context,
        }
        prefetch = self.resolve_prefetch(service, context)
        if prefetch:
            body["prefetch"] = prefetch
        return body

    async def fire_hook(self, hook: str, *, patient_id: str,
                        encounter_id: str | None = None, user_id: str | None = None,
                        extra_context: dict | None = None) -> dict:
        if not self.services:
            # Discovery may have failed at startup (CDS service not up yet) -- retry lazily.
            await self.refresh()
        service = self.service_for(hook)
        if service is None:
            entry = {"hook": hook, "serviceId": None, "at": _now_iso(), "cards": [],
                     "request": None, "response": None,
                     "error": f"no CDS service advertises the '{hook}' hook "
                              f"({self.discovery_error or 'discovery is empty'})"}
            self._record(entry)
            return entry

        body = self.build_request(hook, service, patient_id=patient_id,
                                  encounter_id=encounter_id, user_id=user_id,
                                  extra_context=extra_context)
        path = f"/cds-services/{service['id']}"
        client, owned = self._acquire()
        entry: dict[str, Any] = {"hook": hook, "serviceId": service["id"], "at": _now_iso(),
                                 "request": body, "response": None, "cards": [],
                                 "error": None, "status": None}
        try:
            response = await client.post(self._url(path), json=body,
                                         timeout=self.settings.hook_timeout_seconds)
            entry["status"] = response.status_code
            response.raise_for_status()
            payload = response.json()
            entry["response"] = payload
            entry["cards"] = payload.get("cards", []) or []
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("hook %s failed: %s", hook, entry["error"])
        finally:
            if owned:
                await client.aclose()
        self._record(entry)
        return entry

    # ---------------------------------------------------------------- feedback
    async def send_feedback(self, service_id: str, card_uuid: str | None,
                            suggestion_uuid: str | None, outcome: str = "accepted") -> dict:
        """Per the CDS Hooks feedback spec. Skipped silently when the card has no uuid."""
        if not card_uuid:
            return {"sent": False, "reason": "card has no uuid"}
        item: dict[str, Any] = {"card": card_uuid, "outcome": outcome,
                                "outcomeTimestamp": _now_iso()}
        if outcome == "accepted" and suggestion_uuid:
            item["acceptedSuggestions"] = [{"id": suggestion_uuid}]
        body = {"feedback": [item]}
        client, owned = self._acquire()
        try:
            response = await client.post(self._url(f"/cds-services/{service_id}/feedback"),
                                         json=body,
                                         timeout=self.settings.hook_timeout_seconds)
            return {"sent": True, "status": response.status_code, "body": body}
        except Exception as exc:
            log.warning("feedback POST failed: %s", exc)
            return {"sent": False, "reason": f"{type(exc).__name__}: {exc}", "body": body}
        finally:
            if owned:
                await client.aclose()

    # ----------------------------------------------------------------- history
    def _record(self, entry: dict) -> None:
        """Store a *redacted* copy: the history is served to the dev panel."""
        self.history.append(redact_entry(entry))
        del self.history[:-MAX_HISTORY]


def _bundle_stub(value: Any) -> dict:
    """Replace a FHIR Bundle with its shape. The contents are PHI."""
    entries = (value or {}).get("entry") or []
    total = (value or {}).get("total")
    return {"resourceType": "Bundle",
            "total": total if isinstance(total, int) else len(entries),
            "_redacted": True}


def _is_bundle(value: Any) -> bool:
    return isinstance(value, dict) and value.get("resourceType") == "Bundle"


def redact_entry(entry: dict) -> dict:
    """Strip tokens and PHI out of a recorded hook exchange.

    The live response handed back to the chart UI keeps the full request; only what
    we *retain* (and re-serve from ``/api/dev/history``) is redacted: the bearer
    token becomes ``***`` and every Bundle -- prefetch or in-context draft orders --
    collapses to a count.
    """
    redacted = dict(entry)
    request = entry.get("request")
    if not isinstance(request, dict):
        redacted["context"] = None
        redacted["prefetchCounts"] = {}
        redacted["request"] = None
        return redacted

    request = copy.deepcopy(request)
    auth = request.get("fhirAuthorization")
    if isinstance(auth, dict) and "access_token" in auth:
        auth["access_token"] = "***"
    prefetch = request.get("prefetch")
    if isinstance(prefetch, dict):
        request["prefetch"] = {k: _bundle_stub(v) for k, v in prefetch.items()}
    context = request.get("context")
    if isinstance(context, dict):
        context = {k: (_bundle_stub(v) if _is_bundle(v) else v) for k, v in context.items()}
        request["context"] = context

    redacted["request"] = request
    redacted["context"] = context if isinstance(context, dict) else None
    redacted["prefetchCounts"] = {k: v.get("total", 0)
                                  for k, v in (request.get("prefetch") or {}).items()}
    return redacted
