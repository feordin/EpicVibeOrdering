"""Inference providers for downtime extraction.

Two implementations of the shared `InferenceProvider` protocol
(`complete_json(system, user, json_schema)`):

* `AnthropicSdkProvider` - the real thing, via the official `anthropic` SDK
  with a forced `emit` tool so the model can only answer in our schema.
* `KeywordFakeProvider` - deterministic, no API key, no network. It reads the
  same prompts the real provider gets (the engine delimits the transcript and
  the template spec with XML-ish tags) and fills them with regex heuristics.
  This is what makes the downtime demo runnable on a laptop with the network
  unplugged, which is exactly the scenario the subsystem exists for.
"""

import json
import re
from typing import Any

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


class AnthropicProviderError(RuntimeError):
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
    return KeywordFakeProvider()
