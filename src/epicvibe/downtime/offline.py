"""Offline-strict mode: prove that nothing leaves the box.

The whole claim of this tool is that it keeps working when the network and the
EHR are gone. That claim is easy to make in a slide and easy to break in a
config file - one `EPICVIBE_DOWNTIME_PROVIDER=anthropic` and the transcript is
on someone else's computer.

So the claim is checked rather than asserted: :func:`offline_report` walks the
four things that could reach off-box (the inference provider, the speech model
and its weights, and the integration engine) and says, per item, whether it is
local. With ``EPICVIBE_DOWNTIME_OFFLINE_STRICT=true`` the app refuses to start
when any of them is not - a loud failure at startup beats a quiet egress at
the bedside.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

from epicvibe.downtime.transcribe import faster_whisper_installed, model_cached

#: Providers whose weights run on this box (or on a host the operator names
#: inside the hospital network). `anthropic` is the hosted one.
LOCAL_PROVIDERS = {"fake", "ollama"}

#: A downtime box is not waiting on a slow handshake; either the engine answers
#: immediately on the LAN or it is not there.
PROBE_TIMEOUT_S = 0.5


class OfflineSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EPICVIBE_DOWNTIME_OFFLINE_", env_file=".env", extra="ignore"
    )

    #: Refuse to start unless every check below is local.
    strict: bool = False


def is_local_host(host: str) -> bool:
    """True for loopback and RFC1918 / link-local addresses, and for names that
    resolve to them.

    "Local" here means "inside the trust boundary of the downtime box", which is
    what the operator actually cares about: the integration engine usually lives
    on a hospital VLAN, not on this machine. A public address is not local even
    if it is reachable.
    """
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return _is_private(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # cannot resolve it offline; do not call it local
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(_is_private(ipaddress.ip_address(a)) for a in addrs)


def _is_private(addr: ipaddress._BaseAddress) -> bool:
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def tcp_reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Can we open a TCP connection to host:port right now?"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def offline_report(settings: Any, whisper_settings: Any = None, strict: bool = False) -> dict:
    """Per-item locality checklist for the status bar and `/api/offline`.

    `settings` is the app's :class:`DowntimeSettings`; every field is read with
    ``getattr`` and a default so this keeps working while the provider settings
    grow. `whisper_settings` is a :class:`TranscribeSettings`-shaped object (the
    transcriber's own), or None when audio is not configured.
    """
    provider = str(getattr(settings, "provider", "") or "")
    provider_local = provider in LOCAL_PROVIDERS

    checks: dict[str, bool] = {"provider_local": provider_local}
    detail: dict[str, str] = {
        "provider_local": (
            f"inference provider {provider!r} runs on this box"
            if provider_local
            else f"inference provider {provider!r} is a hosted model - the transcript leaves the box"
        )
    }

    if provider == "ollama":
        base = str(getattr(settings, "ollama_base_url", "") or "")
        ollama_host = urlsplit(base).hostname or ""
        if not is_local_host(ollama_host):
            checks["provider_local"] = False
            detail["provider_local"] = (
                f"ollama_base_url points at {ollama_host or base!r}, which is outside "
                "this machine's trust boundary"
            )

    installed = faster_whisper_installed()
    cached = bool(whisper_settings is not None and installed and model_cached(whisper_settings))
    checks["whisper_installed"] = installed
    detail["whisper_installed"] = (
        "faster-whisper is installed" if installed
        else 'faster-whisper is not installed (pip install -e ".[audio]")'
    )
    checks["whisper_model_cached"] = cached
    detail["whisper_model_cached"] = (
        "Whisper weights are on disk" if cached
        else "Whisper weights are not cached - the first transcription would download them"
    )

    host = str(getattr(settings, "engine_host", "") or "")
    port = int(getattr(settings, "engine_port", 0) or 0)
    engine_local = is_local_host(host)
    reachable = engine_local and tcp_reachable(host, port)
    checks["engine_reachable"] = reachable
    if not engine_local:
        detail["engine_reachable"] = (
            f"integration engine {host}:{port} is not on a loopback or private address"
        )
    else:
        detail["engine_reachable"] = (
            f"integration engine reachable at {host}:{port}" if reachable
            else f"nothing is listening on {host}:{port} (start the mock engine)"
        )

    not_local = [name for name, ok in checks.items() if not ok]
    return {
        "strict": bool(strict),
        "provider": provider,
        **checks,
        "all_local": not not_local,
        "not_local": not_local,
        "detail": detail,
    }


def enforce_strict(report: dict) -> None:
    """Raise if strict mode is on and something is not local."""
    if report.get("all_local"):
        return
    detail = report.get("detail", {})
    items = "\n".join(f"  - {n}: {detail.get(n, n)}" for n in report.get("not_local", []))
    raise RuntimeError(
        "EPICVIBE_DOWNTIME_OFFLINE_STRICT is set but this deployment is not fully "
        f"local:\n{items}\n"
        "Fix the items above, or unset the strict flag if you accept the egress."
    )
