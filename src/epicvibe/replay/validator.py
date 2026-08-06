"""Validator encoding Epic's documented CDS Hooks response rules.

`validate_epic_response(response, hook)` returns a list of human-readable
violation strings; an empty list means the response is valid.
"""

from epicvibe.catalog.models import ORDERSET_SYSTEM, PREF_LIST_SYSTEM

ALLOWED_INDICATORS = {"info", "warning", "critical"}
ALLOWED_ACTION_TYPES = {"create", "delete"}
ALLOWED_RESOURCE_TYPES = {"MedicationRequest", "ServiceRequest"}
ALLOWED_LINK_TYPES = {"absolute", "smart"}
ALLOWED_SYSTEMS = {
    PREF_LIST_SYSTEM,
    ORDERSET_SYSTEM,
    "http://www.nlm.nih.gov/research/umls/rxnorm",
    "http://hl7.org/fhir/sid/ndc",
    "http://loinc.org",
}
ALLOWED_TOP_LEVEL_KEYS = {"cards", "systemActions"}


def validate_epic_response(response: dict, hook: str) -> list[str]:
    violations: list[str] = []

    if not isinstance(response, dict):
        return [f"[{hook}] response must be a JSON object"]

    unknown_keys = set(response.keys()) - ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        violations.append(
            f"[{hook}] unknown top-level key(s): {sorted(unknown_keys)}")

    cards = response.get("cards")
    if not isinstance(cards, list):
        violations.append(f"[{hook}] 'cards' must be present and a list")
        cards = []

    if "systemActions" in response:
        violations.append(
            f"[{hook}] systemActions requires ServiceRequest.Update (Unsigned Order) "
            "annotation support; not emitted by this service")

    for i, card in enumerate(cards):
        violations.extend(_validate_card(card, hook, i))

    return violations


def _prefix(hook: str, i: int, suffix: str = "") -> str:
    return f"[{hook}] card[{i}]{suffix}"


def _validate_card(card: dict, hook: str, i: int) -> list[str]:
    violations: list[str] = []
    if not isinstance(card, dict):
        return [f"{_prefix(hook, i)} must be an object"]

    summary = card.get("summary")
    if not isinstance(summary, str) or len(summary) > 140:
        violations.append(f"{_prefix(hook, i)} summary must be a string of <=140 characters")

    indicator = card.get("indicator")
    if indicator not in ALLOWED_INDICATORS:
        violations.append(
            f"{_prefix(hook, i)} indicator must be one of {sorted(ALLOWED_INDICATORS)}")

    source = card.get("source")
    if not isinstance(source, dict) or not source.get("label"):
        violations.append(f"{_prefix(hook, i)} source must be an object with a 'label'")

    suggestions = card.get("suggestions", [])
    if suggestions and card.get("selectionBehavior") != "any":
        violations.append(
            f"{_prefix(hook, i)} selectionBehavior must be 'any' when suggestions are present")

    for j, suggestion in enumerate(suggestions):
        violations.extend(_validate_suggestion(suggestion, hook, i, j))

    for link in card.get("links", []) or []:
        if link.get("type") not in ALLOWED_LINK_TYPES:
            violations.append(
                f"{_prefix(hook, i)} link type must be one of {sorted(ALLOWED_LINK_TYPES)}")

    return violations


def _validate_suggestion(suggestion: dict, hook: str, i: int, j: int) -> list[str]:
    violations: list[str] = []
    label = f"{_prefix(hook, i)} suggestion[{j}]"
    if not isinstance(suggestion, dict):
        return [f"{label} must be an object"]

    if not isinstance(suggestion.get("uuid"), str) or not suggestion.get("uuid"):
        violations.append(f"{label} uuid must be a non-empty string")

    if not isinstance(suggestion.get("label"), str) or not suggestion.get("label"):
        violations.append(f"{label} label must be a non-empty string")

    actions = suggestion.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        violations.append(f"{label} actions must be a list with EXACTLY one element")
        actions = []

    for k, action in enumerate(actions):
        violations.extend(_validate_action(action, hook, i, j, k))

    return violations


def _validate_action(action: dict, hook: str, i: int, j: int, k: int) -> list[str]:
    violations: list[str] = []
    label = f"{_prefix(hook, i)} suggestion[{j}] action[{k}]"
    if not isinstance(action, dict):
        return [f"{label} must be an object"]

    action_type = action.get("type")
    if action_type not in ALLOWED_ACTION_TYPES:
        violations.append(f"{label} type must be one of {sorted(ALLOWED_ACTION_TYPES)}")

    if action_type != "create":
        return violations

    resource = action.get("resource")
    if not isinstance(resource, dict):
        violations.append(f"{label} resource object must be present for a create action")
        return violations

    resource_type = resource.get("resourceType")
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        violations.append(
            f"{label} resourceType must be one of {sorted(ALLOWED_RESOURCE_TYPES)}")

    subject_ref = (resource.get("subject") or {}).get("reference", "")
    if not isinstance(subject_ref, str) or not subject_ref.startswith("Patient/") or subject_ref == "Patient/":
        violations.append(f"{label} resource.subject.reference must match 'Patient/{{...}}'")

    if not resource.get("status"):
        violations.append(f"{label} resource.status must be present")
    if not resource.get("intent"):
        violations.append(f"{label} resource.intent must be present")

    concept = resource.get("medicationCodeableConcept") or resource.get("code") or {}
    coding = concept.get("coding") or []
    if not coding:
        violations.append(f"{label} resource coding list must be non-empty")
    for c in coding:
        system = c.get("system")
        if system not in ALLOWED_SYSTEMS:
            violations.append(
                f"{label} coding system '{system}' not in ALLOWED_SYSTEMS")

    return violations
