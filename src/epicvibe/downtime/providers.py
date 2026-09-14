"""Inference providers for downtime extraction.

Three implementations of the shared `InferenceProvider` protocol
(`complete_json(system, user, json_schema)`):

* `AnthropicSdkProvider` - the hosted model, via the official `anthropic` SDK
  with a forced `emit` tool so the model can only answer in our schema.
* `OllamaProvider` - a model running on this box via Ollama, using its
  structured-output `format` parameter (a JSON schema) to pin the response
  shape. Local weights, so the transcript never leaves the hospital.
* `KeywordFakeProvider` - deterministic, no API key, no network. It reads the
  same prompts the real provider gets (the engine delimits the transcript and
  the template spec with XML-ish tags) and fills them with regex heuristics.
  This is what makes the downtime demo runnable on a laptop with the network
  unplugged, which is exactly the scenario the subsystem exists for.
"""

import copy
import json
import re
import time
from typing import Any

import httpx

from epicvibe.downtime.config import DowntimeSettings

# --- prompt delimiters shared with engine.py -------------------------------

TRANSCRIPT_OPEN = "<transcript>"
TRANSCRIPT_CLOSE = "</transcript>"
TEMPLATES_OPEN = "<templates>"
TEMPLATES_CLOSE = "</templates>"
SPEC_OPEN = "<template_spec>"
SPEC_CLOSE = "</template_spec>"


def _between(text: str, open_tag: str, close_tag: str) -> str:
    start = text.find(open_tag)
    end = text.find(close_tag)
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start + len(open_tag): end].strip()


# ---------------------------------------------------------------------------
# Anthropic SDK provider
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Any provider failure the capture UI should surface verbatim to the clinician."""


class AnthropicProviderError(ProviderError):
    pass


class AnthropicSdkProvider:
    """`InferenceProvider` backed by the official Anthropic Python SDK.

    Uses a single forced tool call so the response is always schema-shaped.
    `strict=True` needs `additionalProperties: false` plus a complete `required`
    list on every object - `schema.strict_json_schema()` produces that - but if
    a particular schema shape is rejected we retry once without strict rather
    than failing the clinician's order capture.
    """

    TOOL_NAME = "emit"
    TOOL_DESCRIPTION = (
        "Emit the structured downtime-ordering result. Call this exactly once."
    )

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5",
        client: Any | None = None,
        max_tokens: int = 16000,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self._strict = True
        if client is not None:
            self._client = client
        else:
            import anthropic

            # A clinician is waiting on this call during a downtime: bounded wait,
            # bounded retries, no indefinite hang on a flaky link.
            self._client = anthropic.AsyncAnthropic(
                api_key=api_key, timeout=30.0, max_retries=2
            )

    def describe(self) -> str:
        return f"anthropic:{self.model}"

    async def warmup(self) -> dict:
        """No-op: a hosted model has nothing to load, and a warm-up call would
        bill a request (and send a token of nothing) for no latency benefit."""
        return {"provider": self.describe(), "loaded": True, "elapsed_s": 0.0,
                "note": "hosted model - nothing to load on this box"}

    def _tools(self, json_schema: dict, strict: bool) -> list[dict]:
        tool: dict[str, Any] = {
            "name": self.TOOL_NAME,
            "description": self.TOOL_DESCRIPTION,
            "input_schema": json_schema,
        }
        if strict:
            tool["strict"] = True
        return [tool]

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        try:
            response = await self._create(system, user, json_schema, self._strict)
        except Exception as exc:  # noqa: BLE001 - narrow below
            if self._strict and _is_bad_request(exc):
                # Some object shapes are rejected under strict; degrade once and
                # remember, so we do not pay the failed round-trip every call.
                self._strict = False
                response = await self._create(system, user, json_schema, False)
            else:
                raise

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise AnthropicProviderError(
                f"model refused the downtime extraction request (category={category!r})"
            )

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and block.name == self.TOOL_NAME:
                value = block.input
                if isinstance(value, str):
                    value = json.loads(value)
                return value

        raise AnthropicProviderError(
            f"no '{self.TOOL_NAME}' tool_use block in response (stop_reason={stop_reason!r})"
        )

    async def _create(self, system: str, user: str, json_schema: dict, strict: bool):
        return await self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=self._tools(json_schema, strict),
            tool_choice={"type": "tool", "name": self.TOOL_NAME},
        )


def _is_bad_request(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 400:
        return True
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a hard dependency
        return False
    return isinstance(exc, anthropic.BadRequestError)


# ---------------------------------------------------------------------------
# Deterministic keyword / regex provider
# ---------------------------------------------------------------------------

_LB_PER_KG = 2.20462

_FREQ_PATTERNS: list[tuple[str, str]] = [
    (r"every\s+(?:four|4)\s+hours?|q4h?\b", "q4h"),
    (r"every\s+(?:six|6)\s+hours?|q6h?\b", "q6h"),
    (r"every\s+(?:eight|8)\s+hours?|q8h?\b", "q8h"),
    (r"every\s+(?:twelve|12)\s+hours?|q12h?\b", "q12h"),
    (r"twice\s+(?:a\s+)?daily|twice\s+a\s+day|\bbid\b", "BID"),
    (r"three\s+times\s+(?:a\s+)?(?:day|daily)|\btid\b", "TID"),
    (r"\bcontinuous|\bdrip\b|\binfusion\b|per\s+hour|an\s+hour|wide\s+open", "continuous"),
    (r"as\s+needed|\bprn\b", "PRN"),
    (r"\bdaily\b|once\s+a\s+day|every\s+day", "daily"),
    (r"\bonce\b|\bnow\b|\bstat\b", "once"),
]

_ROUTE_PATTERNS: list[tuple[str, str]] = [
    (r"\biv\b|intravenous", "IV"),
    (r"by\s+mouth|\bpo\b|oral|chewable|sublingual|\bsl\b", "PO"),
    (r"intramuscular|\bim\b", "IM"),
    (r"subcutaneous|\bsc\b|\bsubq\b", "SC"),
    (r"inhaled|nebulize", "inhaled"),
]

_UNIT_MAP = {
    "mg": "mg", "milligram": "mg", "milligrams": "mg",
    "g": "g", "gram": "g", "grams": "g",
    "mcg": "mcg", "microgram": "mcg", "micrograms": "mcg",
    "unit": "units", "units": "units",
    "ml": "mL", "mls": "mL", "milliliter": "mL", "milliliters": "mL",
    "meq": "mEq", "milliequivalent": "mEq", "milliequivalents": "mEq",
}

_NEGATORS = (
    "hold the", "hold ", "skip the", "skip ", "do not start", "don't start",
    "no ", "not ", "hold off on", "without ",
)

_STOPWORDS = {
    "with", "and", "the", "for", "per", "plasma", "serum", "order", "panel",
    "hour", "hours", "sets", "set", "draw", "before", "views", "view", "level",
    "test", "type", "high", "sensitivity", "initial", "repeat", "point",
    "care", "strict", "continuous", "daily", "same", "scale", "titrate",
    "added", "when", "under", "above", "until", "from", "recommendation",
    "counseling", "comprehensive", "quantitative", "reflex", "referral",
}


# Sentence splitting must not break on a title, or "Mr. Bennett" lands in two
# different "sentences" and the evidence quote for the name disappears.
_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "jr.", "sr.", "no.")


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        merged: list[str] = []
        for chunk in re.split(r"(?<=[.!?])\s+", line):
            if merged and merged[-1].lower().endswith(_ABBREVIATIONS):
                merged[-1] = f"{merged[-1]} {chunk}"
            else:
                merged.append(chunk)
        parts.append(line)
        parts.extend(c.strip() for c in merged if c.strip())
    return parts


def _find_sentence(sentences: list[str], needle: str) -> str | None:
    """Shortest sentence (or line) containing `needle`, verbatim."""
    low = needle.lower()
    hits = [s for s in sentences if low in s.lower()]
    return min(hits, key=len) if hits else None


def _terms(order: dict) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", order.get("display", ""))
    terms = [w.lower() for w in words if w.lower() not in _STOPWORDS]
    for piece in order.get("order_id", "").split("-"):
        if len(piece) > 2 and piece.lower() not in terms:
            terms.append(piece.lower())
    return terms


class KeywordFakeProvider:
    """Deterministic stand-in for the model. No key, no network, no surprises."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def describe(self) -> str:
        return "fake:keyword"

    async def warmup(self) -> dict:
        """Nothing to load: the extractor is regexes compiled at import time."""
        return {"provider": self.describe(), "loaded": True, "elapsed_s": 0.0,
                "note": "deterministic extractor - no weights to load"}

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user})
        transcript = _between(user, TRANSCRIPT_OPEN, TRANSCRIPT_CLOSE)
        if SPEC_OPEN in user:
            spec = json.loads(_between(user, SPEC_OPEN, SPEC_CLOSE))
            return self._fill(spec, transcript)
        summaries = json.loads(_between(user, TEMPLATES_OPEN, TEMPLATES_CLOSE))
        return self._select(summaries, transcript)

    # -- step 1: template selection -----------------------------------------

    def _select(self, summaries: list[dict], transcript: str) -> dict:
        low = transcript.lower()
        scored: list[tuple[int, str, list[str]]] = []
        for s in summaries:
            hits = [
                term for term in
                {k.lower() for k in s.get("keywords", [])}
                | {i.lower() for i in s.get("indications", [])}
                if term and term in low
            ]
            scored.append((len(hits), s["template_id"], sorted(hits)))
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0]
        confidence = "high" if best[0] >= 5 else "medium" if best[0] >= 2 else "low"
        return {
            "template_id": best[1],
            "confidence": confidence,
            "rationale": (
                f"{best[0]} template keyword(s) present in the transcript: "
                + ", ".join(best[2][:8])
            ) if best[0] else "no keyword overlap; defaulting to first template by id",
            "alternatives": [
                {"template_id": tid, "reason": f"{n} keyword match(es): " + ", ".join(hits[:5])}
                for n, tid, hits in scored[1:4] if n > 0
            ],
        }

    # -- step 2: template filling -------------------------------------------

    def _fill(self, spec: dict, transcript: str) -> dict:
        sentences = _sentences(transcript)
        low = transcript.lower()

        patient_fields = [
            self._patient_field(f, transcript, low, sentences)
            for f in spec.get("patient_fields", [])
        ]
        orders = [self._order(o, transcript, low, sentences) for o in spec.get("orders", [])]

        unresolved = [
            f["field_id"] for f, filled in zip(spec.get("patient_fields", []), patient_fields)
            if f.get("required") and not filled["value"]
        ]
        return {
            "template_id": spec["template_id"],
            "patient_fields": patient_fields,
            "orders": orders,
            "unresolved": unresolved,
            "warnings": ["filled by the deterministic keyword provider (no model was called)"],
        }

    def _patient_field(self, field: dict, text: str, low: str, sentences: list[str]) -> dict:
        fid = field["field_id"]
        handler = getattr(self, f"_pf_{fid.replace('-', '_')}", None)
        value: str | None = None
        evidence: str | None = None
        if handler is not None:
            value, evidence = handler(text, low, sentences)
        confidence = "high" if value and evidence else "medium" if value else "low"
        return {"field_id": fid, "value": value, "evidence": evidence, "confidence": confidence}

    # each _pf_* returns (value, evidence)

    def _pf_patient_name(self, text, low, sentences):
        for pattern in (
            r"(?:the\s+)?patient\s+is\s+((?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)+)",
            r"this\s+is\s+((?:Mr\.|Ms\.|Mrs\.)\s*[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)*)",
            r"((?:Mr\.|Ms\.|Mrs\.)\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)*)",
        ):
            m = re.search(pattern, text)
            if m:
                name = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.)\s*", "", m.group(1)).strip()
                return name, _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_dob(self, text, low, sentences):
        m = re.search(r"date of birth(?:\s+is)?\s+(\d{4}-\d{2}-\d{2})", text, re.I)
        if m:
            return m.group(1), _find_sentence(sentences, m.group(0))
        m = re.search(r"\b(born|date of birth)[^.\n]{0,20}?(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
        if m:
            return m.group(2), _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_sex(self, text, low, sentences):
        m = re.search(r"\b(male|female)\b", low)
        if m:
            return m.group(1), _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_mrn(self, text, low, sentences):
        m = re.search(r"\bmrn\D{0,10}([A-Z0-9\-]{4,})", text, re.I)
        if m:
            return m.group(1), _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_weight_kg(self, text, low, sentences):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:kg|kilograms?|kilos)\b", low)
        if m:
            return m.group(1), _find_sentence(sentences, m.group(0))
        m = re.search(r"weighs?\s+(\d+(?:\.\d+)?)\s*(?:lbs?|pounds)\b", low)
        if m:
            return f"{float(m.group(1)) / _LB_PER_KG:.1f}", _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_allergies(self, text, low, sentences):
        m = re.search(
            r"(?:i'?m\s+)?allergic to\s+([a-zA-Z\- ]+?)(?:[,.\n–—]|\s+-\s)", text, re.I
        )
        if m:
            return m.group(1).strip(), _find_sentence(sentences, m.group(0))
        m = re.search(r"no known (?:drug )?allergies", text, re.I)
        if m:
            return "NKDA", _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_ordering_provider(self, text, low, sentences):
        m = re.search(r"\bDr\.\s+([A-Z][a-zA-Z'\-]+)", text)
        if m:
            return f"Dr. {m.group(1)}", _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_encounter_location(self, text, low, sentences):
        for pattern in (
            r"(?:Exam\s+Room|Resuscitation\s+Bay|MICU\s+Room|ED\s+Bay|ED\s+Room|Room|Bay)\s+\d+",
            r"\bresus\s+bay\s+\w+",
        ):
            m = re.search(pattern, text, re.I)
            if m:
                return m.group(0), _find_sentence(sentences, m.group(0))
        return None, None

    def _pf_chief_complaint(self, text, low, sentences):
        for s in sentences:
            if s.upper().startswith("PATIENT:"):
                value = s.split(":", 1)[1].strip()
                if value:
                    return value[:180], s
        return None, None

    # -- orders --------------------------------------------------------------

    def _order(self, order: dict, text: str, low: str, sentences: list[str]) -> dict:
        terms = _terms(order)
        matched = next((t for t in terms if t in low), None)
        negated = bool(matched) and self._negated(low, matched)
        selected = bool(matched) and not negated
        if order.get("default_selected") and not negated:
            selected = True

        context = _find_sentence(sentences, matched) if matched else None
        if negated:
            rationale = f"explicitly deferred in the transcript ('{matched}')"
        elif matched:
            rationale = f"'{matched}' stated in the transcript"
        elif order.get("default_selected"):
            rationale = "template default; not explicitly stated in the transcript"
        else:
            rationale = "not mentioned in the transcript"

        fields = [
            self._order_field(f, order, context, low, sentences)
            for f in order.get("fields", [])
        ]
        return {
            "order_id": order["order_id"],
            "selected": selected,
            "rationale": rationale,
            "fields": fields,
        }

    def _negated(self, low: str, term: str) -> bool:
        for m in re.finditer(re.escape(term), low):
            window = low[max(0, m.start() - 22): m.start()]
            if any(window.rstrip().endswith(n.strip()) or n in window for n in _NEGATORS):
                return True
        return False

    def _order_field(
        self, field: dict, order: dict, context: str | None, low: str, sentences: list[str]
    ) -> dict:
        fid = field["field_id"]
        defaults = order.get("defaults", {}) or {}
        value: str | None = None
        evidence: str | None = None
        ctx_low = (context or "").lower()

        if fid == "dose":
            m = re.search(r"(\d+(?:\.\d+)?)\s*(mg|milligrams?|g|grams?|mcg|micrograms?|"
                          r"units?|ml|mls|milliliters?|meq|milliequivalents?)\b", ctx_low)
            if m:
                value, evidence = m.group(1), context
        elif fid == "dose_units":
            m = re.search(r"\d+(?:\.\d+)?\s*(mg|milligrams?|g|grams?|mcg|micrograms?|"
                          r"units?|ml|mls|milliliters?|meq|milliequivalents?)\b", ctx_low)
            if m:
                value, evidence = _UNIT_MAP.get(m.group(1), None), context
        elif fid == "route":
            for pattern, route in _ROUTE_PATTERNS:
                if re.search(pattern, ctx_low):
                    value, evidence = route, context
                    break
        elif fid == "frequency":
            for pattern, freq in _FREQ_PATTERNS:
                if re.search(pattern, ctx_low):
                    value, evidence = freq, context
                    break
        elif fid == "priority":
            if "stat" in ctx_low or "immediately" in ctx_low or "right now" in ctx_low:
                value, evidence = "STAT", context
            elif "urgent" in ctx_low:
                value, evidence = "urgent", context
            elif "routine" in ctx_low:
                value, evidence = "routine", context
        elif fid == "specimen_notes":
            if "before" in ctx_low and "antibiotic" in ctx_low:
                value, evidence = "draw before antibiotics", context
        elif fid == "indication":
            value, evidence = defaults.get("indication"), None
            if not value and context:
                value, evidence = order.get("display", ""), context

        if value is None and defaults.get(fid):
            value, evidence = defaults[fid], None

        confidence = "high" if value and evidence else "medium" if value else "low"
        return {"field_id": fid, "value": value, "evidence": evidence, "confidence": confidence}


# ---------------------------------------------------------------------------
# Ollama provider (local weights)
# ---------------------------------------------------------------------------


class OllamaProviderError(ProviderError):
    pass


#: `/api/ps` is a bookkeeping read, not inference - it answers instantly or the
#: daemon is not there, and a status page must not block on it.
PS_TIMEOUT_S = 5.0


# Keys that describe the node itself rather than the value it constrains, and so
# must not be copied down into each generated `anyOf` branch.
_NODE_KEYWORDS = {"anyOf", "oneOf", "allOf", "title", "description", "default",
                  "$ref", "$defs", "required", "properties", "items"}


def relax_json_schema(schema: dict) -> dict:
    """Undo the Anthropic `strict: true` post-processing, for Ollama.

    Ollama lowers the `format` schema into a llama.cpp GBNF grammar. That
    converter understands plain JSON Schema - `anyOf`, `enum`, `$ref` - but not
    the strict-tool dialect `schema.strict_json_schema()` emits: it has no
    notion of `additionalProperties: false`, and a nullable union written as a
    *list* of types (`"type": ["string", "null"]`) is not a shape it lowers.
    So we walk the schema back to the plain pydantic form: list-typed nodes
    become `anyOf`, and `additionalProperties: false` is dropped.

    `required` is deliberately left as strictification set it - every property.
    The engine's post-validation fills in whatever the model omits, but a
    grammar that forces every key out of a small model yields far more filled
    fields than one that lets it answer `{}`.
    """
    out = copy.deepcopy(schema)
    _relax(out)
    return out


def _relax(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _relax(item)
        return
    if not isinstance(node, dict):
        return

    # Only the strict-mode `false` is meaningless to a grammar. A real subschema
    # under `additionalProperties` constrains extra keys and must survive.
    if node.get("additionalProperties") is False:
        node.pop("additionalProperties")

    types = node.get("type")
    if isinstance(types, list):
        node.pop("type")
        siblings = {k: v for k, v in node.items() if k not in _NODE_KEYWORDS}
        for key in siblings:
            node.pop(key, None)
        node["anyOf"] = [
            {"type": t} if t == "null" else {"type": t, **siblings} for t in types
        ]

    for value in list(node.values()):
        _relax(value)


class OllamaProvider:
    """`InferenceProvider` backed by a model served by a local Ollama daemon.

    Ollama runs on this box (or at least inside the hospital network), so unlike
    the hosted provider it is *not* gated on `allow_phi_to_model`: the transcript
    never leaves the machine, which is the whole point during a downtime. The
    trade is latency - a large model on CPU-only hardware can take minutes for a
    single fill - so the timeout defaults are deliberately generous.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "gemma4:26b",
        timeout: float = 300.0,
        keep_alive: str = "10m",
        num_ctx: int = 16384,
        think: bool | None = False,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.think = think
        self._client = client
        self._owns_client = client is None
        # Flipped off permanently the first time a daemon or model rejects the
        # `think` key, so we do not pay a failed round-trip on every call.
        self._send_think = think is not None

    def describe(self) -> str:
        return f"ollama:{self.model}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def warmup(self) -> dict:
        """Pull the weights into RAM now, so the clinician does not pay for it.

        `/api/generate` with an *empty* prompt is Ollama's documented load-only
        call: the daemon resolves the model, maps it into memory and returns
        immediately without generating a token. With `keep_alive` set the model
        then stays resident, which is the whole point of doing this before the
        room rather than during it - the first real fill on a 26B model is
        otherwise a minute of load time the clinician watches.
        """
        url = f"{self.base_url}/api/generate"
        payload = {"model": self.model, "prompt": "", "keep_alive": self.keep_alive}
        started = time.perf_counter()
        try:
            response = await self._http().post(url, json=payload, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise OllamaProviderError(
                f"{self.describe()} did not finish loading within {self.timeout:g}s at {url}. "
                "A large model loading from cold disk can exceed this - raise "
                "EPICVIBE_DOWNTIME_OLLAMA_TIMEOUT_SECONDS."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaProviderError(
                f"cannot reach the Ollama daemon at {url} ({type(exc).__name__}: {exc}). "
                "Is `ollama serve` running, and is EPICVIBE_DOWNTIME_OLLAMA_BASE_URL correct?"
            ) from exc

        if response.status_code != 200:
            raise OllamaProviderError(
                f"Ollama returned HTTP {response.status_code} warming model {self.model!r}: "
                f"{response.text[:400]}"
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("error"):
            raise OllamaProviderError(
                f"Ollama error warming model {self.model!r}: {body['error']}"
            )
        return {
            "provider": self.describe(),
            "loaded": True,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "keep_alive": self.keep_alive,
        }

    async def is_loaded(self) -> bool:
        """Is this model resident right now? `/api/ps` is Ollama's `ps`."""
        url = f"{self.base_url}/api/ps"
        try:
            response = await self._http().get(url, timeout=PS_TIMEOUT_S)
        except httpx.HTTPError:
            # A status probe must never be the thing that breaks the status
            # page: an unreachable daemon simply means "not loaded".
            return False
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        names = {str(m.get("name") or m.get("model") or "")
                 for m in (body or {}).get("models") or []}
        # `ollama ps` reports the fully-qualified tag; a model configured as
        # `gemma4` comes back as `gemma4:latest`, so match the bare name too.
        return any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                   and self.model in (n, n.split(":")[0]) for n in names)

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        schema = relax_json_schema(json_schema)
        content = await self._chat(system, user, schema)
        try:
            return _as_object(content)
        except ValueError as first:
            # Small models occasionally wrap the object in prose or a fence even
            # with a grammar applied. One nudge, then fail loudly.
            nudge = (
                f"{user}\n\nYour previous reply was not valid JSON. Return ONLY the JSON "
                "object that matches the schema. No prose, no markdown fence, no commentary."
            )
            retry = await self._chat(system, nudge, schema)
            try:
                return _as_object(retry)
            except ValueError as second:
                raise OllamaProviderError(
                    f"{self.describe()} did not return a JSON object "
                    f"(first attempt: {first}; retry: {second}). "
                    f"Raw retry response: {retry[:400]!r}"
                ) from second

    async def _chat(self, system: str, user: str, schema: dict) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        if self._send_think:
            # Reasoning models put their scratchpad in `message.thinking`, not in
            # `message.content`. Combined with a grammar-constrained `format`,
            # one of them will happily spend its entire context thinking and
            # return an empty content string with done_reason "length" - which is
            # the single most confusing failure mode of this provider. Turning
            # thinking off is what makes a 26B reasoning model usable here at all
            # (measured: empty after 25k tokens, versus a complete fill in ~60s).
            payload["think"] = self.think
        url = f"{self.base_url}/api/chat"
        try:
            response = await self._http().post(url, json=payload, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise OllamaProviderError(
                f"{self.describe()} timed out after {self.timeout:g}s at {url}. "
                "A large model on CPU can exceed this - raise "
                "EPICVIBE_DOWNTIME_OLLAMA_TIMEOUT_SECONDS or use a smaller model."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaProviderError(
                f"cannot reach the Ollama daemon at {url} ({type(exc).__name__}: {exc}). "
                "Is `ollama serve` running, and is EPICVIBE_DOWNTIME_OLLAMA_BASE_URL correct?"
            ) from exc

        if response.status_code != 200:
            if self._send_think and "think" in response.text.lower():
                # This model has no thinking mode to turn off. Drop the key and
                # retry once; a non-reasoning model never had the problem it solves.
                self._send_think = False
                return await self._chat(system, user, schema)
            raise OllamaProviderError(
                f"Ollama returned HTTP {response.status_code} for model {self.model!r}: "
                f"{response.text[:400]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise OllamaProviderError(
                f"Ollama returned a non-JSON envelope: {response.text[:200]!r}"
            ) from exc

        if isinstance(body, dict) and body.get("error"):
            raise OllamaProviderError(f"Ollama error for model {self.model!r}: {body['error']}")

        message = (body or {}).get("message") or {}
        content = message.get("content") or ""
        if not content and message.get("thinking"):
            raise OllamaProviderError(
                f"{self.describe()} returned only reasoning tokens and no answer "
                f"(done_reason={body.get('done_reason')!r}, {body.get('eval_count')} tokens). "
                "Set EPICVIBE_DOWNTIME_OLLAMA_THINK=false, or raise "
                "EPICVIBE_DOWNTIME_OLLAMA_NUM_CTX so it has room to finish."
            )
        return content


def _as_object(content: str) -> dict:
    """Parse a chat response into a JSON object, tolerating a markdown fence."""
    text = (content or "").strip()
    if not text:
        raise ValueError("empty response content")
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("response contained no JSON object") from None
        try:
            value = json.loads(text[start: end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------


def build_provider(settings: DowntimeSettings):
    """Pick a provider from settings. Falls back loudly, never silently."""
    if settings.provider == "anthropic":
        if not settings.allow_phi_to_model:
            raise ValueError(
                "EPICVIBE_DOWNTIME_PROVIDER=anthropic sends the raw encounter transcript "
                "(patient name, DOB, complaint) to a hosted model. Set "
                "EPICVIBE_DOWNTIME_ALLOW_PHI_TO_MODEL=true to accept that, or leave the "
                "provider on 'fake' for the on-box keyword extractor."
            )
        key = settings.resolved_api_key()
        if not key:
            raise ValueError(
                "EPICVIBE_DOWNTIME_PROVIDER=anthropic but no API key found "
                "(set EPICVIBE_DOWNTIME_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)"
            )
        return AnthropicSdkProvider(api_key=key, model=settings.model)
    if settings.provider == "ollama":
        # No PHI gate: Ollama runs on this box, so the transcript never leaves it.
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout=settings.ollama_timeout_seconds,
            keep_alive=settings.ollama_keep_alive,
            num_ctx=settings.ollama_num_ctx,
            think=settings.ollama_think,
        )
    return KeywordFakeProvider()
