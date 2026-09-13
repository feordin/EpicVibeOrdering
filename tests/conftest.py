import os
from urllib.parse import urlsplit

import pytest

# Loopback, plus the RFC 6761 `.test` TLD that the in-test fake EHRs use over an
# ASGI transport (it never resolves, so it can never become a real network call).
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def _is_local(host: str) -> bool:
    host = (host or "").lower()
    return host in LOOPBACK_HOSTS or host.endswith(".test")


def _set_env() -> None:
    """Overwrite (never `setdefault`) so a dev `.env` or an ambient shell
    variable cannot turn the suite's outbound calls back on."""
    os.environ["EPICVIBE_FHIR_PULL_ENABLED"] = "false"
    os.environ["EPICVIBE_INFERENCE_PROVIDER"] = "fake"


_set_env()


@pytest.fixture(autouse=True, scope="session")
def _no_outbound_fhir_pull():
    """Keep the suite hermetic: the hook job's best-effort live FHIR pull is a
    network call, so it is off by default under test.  Tests that exercise the
    pull call `fetch_missing` directly with an injected client."""
    _set_env()
    yield


@pytest.fixture(autouse=True)
def _guard_fhir_pull_network(monkeypatch):
    """Fail the test if the FHIR pull would reach a real network endpoint.

    Both call sites that could escape swallow exceptions (the hook job's
    `except Exception`, and `asyncio.gather(return_exceptions=True)`), so
    violations are recorded and asserted at teardown as well as raised.
    """
    from epicvibe.proposal import fhir_pull

    violations: list[str] = []

    def _check(base: str, client) -> None:
        host = (urlsplit(base or "").hostname or "").lower()
        if client is None:
            violations.append(f"fhir pull with no injected client (server={base!r})")
        if host and not _is_local(host):
            violations.append(f"fhir pull against non-loopback host {host!r}")

    real_fetch_missing = fhir_pull.fetch_missing
    real_fetch_json = fhir_pull.fetch_json

    async def guarded_fetch_missing(summary, fhir_server, fhir_authorization, patient_id,
                                    encounter_id=None, client=None, **kwargs):
        if fhir_server:
            _check(fhir_server, client)
            if violations:
                raise AssertionError(violations[-1])
        return await real_fetch_missing(summary, fhir_server, fhir_authorization, patient_id,
                                        encounter_id, client, **kwargs)

    async def guarded_fetch_json(client, base, path, headers, timeout):
        host = (urlsplit(base or "").hostname or "").lower()
        if not _is_local(host):
            violations.append(f"fhir read against non-loopback host {host!r}")
            raise AssertionError(violations[-1])
        return await real_fetch_json(client, base, path, headers, timeout)

    monkeypatch.setattr(fhir_pull, "fetch_missing", guarded_fetch_missing)
    monkeypatch.setattr(fhir_pull, "fetch_json", guarded_fetch_json)
    yield
    assert not violations, "outbound FHIR network access during tests: " + "; ".join(violations)
