"""HL7 v2.5.1 ORM^O01 construction for downtime order write-back.

The FHIR spike (`docs/spikes/fhir-order-writeback.md`) ruled out FHIR order
creates outside CDS Hooks, so the primary write-back channel is an inbound
ORM^O01 interface on the customer's integration engine (Epic Bridges). This
module hand-rolls those messages - no HL7 library - and parses the ACK^O01 that
comes back.

Segment layout per signed order set:

    MSH  message header
    PID  patient identity (real MRN when known, else a DT-xxxxxxxx downtime ID)
    PV1  encounter location / attending
    AL1  one per allergy
    then, per selected order:
    ORC  order control (NW), placer number, ordering provider
    OBR  the order itself: code, priority, requested date/time, quantity/timing
         (OBR-27) and, for non-medication orders, the clinical indication in
         OBR-31 (Reason for Study) - Bridges reads OBR-31, not ORC-16, for a
         diagnostic order's reason
    RXO  medications only: give code, dose, units, administration instructions
    RXR  medications only: route
    NTE  indication and the transcript evidence that justified the order
"""

import re
import uuid
from datetime import datetime
from typing import Any

SEG = "\r"
FIELD = "|"
COMP = "^"
REP = "~"
ESC = "\\"
SUB = "&"
ENCODING_CHARACTERS = "^~\\&"

HL7_VERSION = "2.5.1"
MESSAGE_TYPE = "ORM^O01^ORM_O01"
PROCESSING_ID = "T"

# Assigning authority used for identities minted during a downtime.
DOWNTIME_ID_TYPE = "DTID"
DOWNTIME_ASSIGNING_AUTHORITY = "EPICVIBE_DOWNTIME"

_ACK_OK = {"AA", "CA"}
_ACK_REJECT = {"AE", "AR", "CE", "CR"}


def escape(value: Any) -> str:
    """Escape HL7 delimiters. Backslash first, or we double-escape the rest."""
    if value is None:
        return ""
    s = str(value)
    s = s.replace(ESC, "\\E\\")
    s = s.replace(FIELD, "\\F\\")
    s = s.replace(COMP, "\\S\\")
    s = s.replace(REP, "\\R\\")
    s = s.replace(SUB, "\\T\\")
    return s.replace("\r", " ").replace("\n", " ")


def unescape(value: str) -> str:
    s = value.replace("\\F\\", FIELD).replace("\\S\\", COMP)
    s = s.replace("\\R\\", REP).replace("\\T\\", SUB)
    return s.replace("\\E\\", ESC)


def hl7_now(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y%m%d%H%M%S")


def new_control_id() -> str:
    """MSH-10. Must be unique per message and <= 20 chars."""
    return f"DT{uuid.uuid4().hex[:16].upper()}"


def downtime_patient_id() -> str:
    return f"DT-{uuid.uuid4().hex[:8]}"


def _segment(name: str, fields: dict[int, str]) -> str:
    """Build a segment from sparse 1-based field positions."""
    if not fields:
        return name
    top = max(fields)
    parts = [fields.get(i, "") for i in range(1, top + 1)]
    return FIELD.join([name, *parts])


def _coded(code: str, text: str, system: str) -> str:
    return COMP.join([escape(code), escape(text), escape(system)])


def _split_name(full: str | None) -> tuple[str, str, str]:
    """'Harold Bennett' -> ('BENNETT', 'HAROLD', ''). Best effort; a downtime
    name is whatever the clinician said out loud."""
    if not full:
        return ("UNKNOWN", "", "")
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if not parts:
        return ("UNKNOWN", "", "")
    if len(parts) == 1:
        return (parts[0].upper(), "", "")
    return (parts[-1].upper(), parts[0].upper(), " ".join(parts[1:-1]).upper())


def _sex(value: str | None) -> str:
    return {"male": "M", "female": "F", "other": "O", "unknown": "U"}.get(
        (value or "").strip().lower(), "U"
    )


def _dob(value: str | None) -> str:
    if not value:
        return ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else ""


def field_values(filled: list[dict] | dict) -> dict[str, str | None]:
    """Flatten a FilledField list (or a plain dict) into {field_id: value}."""
    if isinstance(filled, dict):
        return {k: (str(v) if v is not None else None) for k, v in filled.items()}
    return {f.get("field_id"): f.get("value") for f in filled if f.get("field_id")}


def field_evidence(filled: list[dict] | dict) -> dict[str, str | None]:
    if isinstance(filled, dict):
        return {}
    return {f.get("field_id"): f.get("evidence") for f in filled if f.get("field_id")}


def _allergy_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    cleaned = raw.strip()
    if cleaned.upper() in {"NKDA", "NKA", "NONE", "NO KNOWN DRUG ALLERGIES"}:
        return []
    return [a.strip() for a in re.split(r"[,;]| and ", cleaned) if a.strip()]


def build_orm(
    order_row: dict,
    template,
    settings,
    *,
    control_id: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Build one ORM^O01 for a signed downtime order set.

    Returns `(message, control_id)`. The message uses \\r segment separators,
    which is what MLLP expects; `hl7.pretty()` is for human display only.
    """
    control_id = control_id or new_control_id()
    ts = hl7_now(now)
    patient = field_values(order_row.get("patient_fields", []))

    mrn = (patient.get("mrn") or "").strip()
    if mrn:
        pid_3 = COMP.join([escape(mrn), "", "", escape(settings.receiving_facility), "MR"])
    else:
        pid_3 = COMP.join([
            escape(order_row.get("downtime_patient_id") or downtime_patient_id()),
            "", "", DOWNTIME_ASSIGNING_AUTHORITY, DOWNTIME_ID_TYPE,
        ])

    last, first, middle = _split_name(patient.get("patient_name"))
    segments = [
        _segment("MSH", {
            1: ENCODING_CHARACTERS,
            2: escape(settings.sending_application),
            3: escape(settings.sending_facility),
            4: escape(settings.receiving_application),
            5: escape(settings.receiving_facility),
            6: ts,
            8: MESSAGE_TYPE,
            9: control_id,
            10: PROCESSING_ID,
            11: HL7_VERSION,
        }),
        _segment("PID", {
            1: "1",
            3: pid_3,
            5: COMP.join([escape(last), escape(first), escape(middle)]),
            7: _dob(patient.get("dob")),
            8: _sex(patient.get("sex")),
        }),
        _segment("PV1", {
            1: "1",
            2: _patient_class(template),
            3: escape(patient.get("encounter_location") or ""),
            7: escape(patient.get("ordering_provider") or ""),
            44: ts,
        }),
    ]

    for i, allergen in enumerate(_allergy_list(patient.get("allergies")), start=1):
        segments.append(_segment("AL1", {1: str(i), 2: "DA", 3: escape(allergen), 4: "UN"}))

    provider = escape(patient.get("ordering_provider") or "")
    set_id = 0
    for filled_order in order_row.get("orders", []):
        if not filled_order.get("selected"):
            continue
        spec = template.order(filled_order.get("order_id", ""))
        if spec is None:
            continue
        set_id += 1
        segments.extend(
            _order_segments(filled_order, spec, set_id, control_id, provider, ts)
        )

    return SEG.join(segments) + SEG, control_id


def _patient_class(template) -> str:
    return {"ED": "E", "inpatient": "I", "ambulatory": "O"}.get(
        getattr(template, "setting", ""), "U"
    )


_PRIORITY_MAP = {"STAT": "S", "urgent": "A", "routine": "R"}


def _order_segments(
    filled_order: dict, spec, set_id: int, control_id: str, provider: str, ts: str
) -> list[str]:
    values = field_values(filled_order.get("fields", []))
    evidence = field_evidence(filled_order.get("fields", []))
    placer = f"{control_id}-{set_id}"
    priority = _PRIORITY_MAP.get((values.get("priority") or "").strip(), "R")
    indication = values.get("indication") or ""
    requested = values.get("requested_datetime") or ""
    universal = _coded(spec.code, spec.display, spec.code_system)
    is_medication = spec.category == "medication"

    orc_fields: dict[int, str] = {
        1: "NW",
        2: escape(placer),
        9: ts,
        10: provider,
        12: provider,
        15: ts,
    }
    if is_medication and indication:
        # ORC-16 (order control code reason) is the right home for a medication's
        # reason; a diagnostic order's reason belongs in OBR-31.
        orc_fields[16] = escape(indication)

    obr_fields: dict[int, str] = {
        1: str(set_id),
        2: escape(placer),
        4: universal,
        5: priority,
        6: escape(requested) or ts,
        16: provider,
        # OBR-27 quantity/timing: quantity^interval^duration^start^end^priority.
        # Engines that ignore the deprecated OBR-5 read the priority from here.
        27: COMP.join(["1", "", "", escape(requested) or ts, "", priority]),
    }
    if indication:
        obr_fields[31] = escape(indication)

    segments = [_segment("ORC", orc_fields), _segment("OBR", obr_fields)]

    if is_medication:
        dose = values.get("dose") or ""
        units = values.get("dose_units") or ""
        frequency = values.get("frequency") or ""
        duration = values.get("duration") or ""
        route = values.get("route") or ""
        instructions = " ".join(p for p in [frequency, duration] if p)
        segments.append(_segment("RXO", {
            1: universal,
            2: escape(dose),
            4: escape(units),
            7: escape(instructions),
        }))
        if route:
            segments.append(_segment("RXR", {1: escape(route)}))

    note_id = 0
    if indication:
        note_id += 1
        segments.append(_segment("NTE", {
            1: str(note_id), 2: "L", 3: escape(f"Indication: {indication}")
        }))
    quote = next((q for q in (evidence.get(k) for k in ("dose", "indication", "priority",
                                                        "frequency", "route")) if q), None)
    if quote:
        note_id += 1
        segments.append(_segment("NTE", {
            1: str(note_id), 2: "L", 3: escape(f"Transcript evidence: {quote}")
        }))
    rationale = filled_order.get("rationale")
    if rationale:
        note_id += 1
        segments.append(_segment("NTE", {
            1: str(note_id), 2: "L", 3: escape(f"Capture rationale: {rationale}")
        }))
    return segments


# ---------------------------------------------------------------------------
# ACK parsing
# ---------------------------------------------------------------------------


def parse_ack(text: str) -> tuple[str, str, str]:
    """Parse an ACK^O01. Returns `(ack_code, message_control_id, ack_text)`.

    `message_control_id` is MSA-2, i.e. the control id of the message being
    acknowledged - that is what we match our sent order against.
    """
    if not text:
        return ("", "", "empty ACK")
    normalized = text.replace("\n", "\r")
    for raw in normalized.split("\r"):
        line = raw.strip("\x0b\x1c\r\n ")
        if not line.startswith("MSA"):
            continue
        parts = line.split(FIELD)
        code = parts[1].strip() if len(parts) > 1 else ""
        control = parts[2].strip() if len(parts) > 2 else ""
        message = unescape(parts[3].strip()) if len(parts) > 3 else ""
        return (code, control, message)
    return ("", "", "no MSA segment in ACK")


def ack_is_accept(code: str) -> bool:
    return code.upper() in _ACK_OK


def ack_is_reject(code: str) -> bool:
    return code.upper() in _ACK_REJECT


def build_ack(
    control_id: str,
    code: str = "AA",
    text: str = "",
    *,
    sending_application: str = "EPIC",
    sending_facility: str = "BRIDGES",
    receiving_application: str = "EPICVIBE",
    receiving_facility: str = "DOWNTIME",
    now: datetime | None = None,
) -> str:
    """Build an ACK^O01. Used by the mock integration engine."""
    ts = hl7_now(now)
    msh = _segment("MSH", {
        1: ENCODING_CHARACTERS,
        2: escape(sending_application),
        3: escape(sending_facility),
        4: escape(receiving_application),
        5: escape(receiving_facility),
        6: ts,
        8: "ACK^O01^ACK",
        9: new_control_id(),
        10: PROCESSING_ID,
        11: HL7_VERSION,
    })
    msa = _segment("MSA", {1: code, 2: escape(control_id), 3: escape(text)})
    return msh + SEG + msa + SEG


# ---------------------------------------------------------------------------
# introspection helpers (UI, logging, tests)
# ---------------------------------------------------------------------------


def segments(message: str) -> list[str]:
    return [s for s in message.replace("\n", "\r").split("\r") if s.strip()]


def segment_counts(message: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for seg in segments(message):
        name = seg[:3]
        counts[name] = counts.get(name, 0) + 1
    return counts


def pretty(message: str) -> str:
    """Newline-separated form for humans. Never send this over MLLP."""
    return "\n".join(segments(message))


def summarize(message: str) -> dict:
    """One-line summary of an inbound message - what the engine logs."""
    segs = segments(message)
    msh = next((s for s in segs if s.startswith("MSH")), "")
    pid = next((s for s in segs if s.startswith("PID")), "")
    msh_parts = msh.split(FIELD)
    pid_parts = pid.split(FIELD)
    return {
        "message_type": msh_parts[8] if len(msh_parts) > 8 else "",
        "control_id": msh_parts[9] if len(msh_parts) > 9 else "",
        "patient_id": (pid_parts[3].split(COMP)[0] if len(pid_parts) > 3 else ""),
        "orc_count": sum(1 for s in segs if s.startswith("ORC")),
        "segments": len(segs),
    }
