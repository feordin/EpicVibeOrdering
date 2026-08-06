"""Epic-shaped CDS Hooks request builders.

Mimics the request dialect Epic's CDS Hooks tutorial documents: an `extension`
block with `com.epic.cdshooks.request.*` keys, PractitionerRole userId, an
Epic-style FHIR scope string, and (for order-select/order-sign) draft orders
that reference a *contained* Medication resource rather than using
medicationCodeableConcept directly.
"""

import json
from pathlib import Path

from epicvibe.catalog.models import PREF_LIST_SYSTEM

_PATIENT_VIEW_FIXTURE = Path("fixtures/hooks/patient_view.json")

EPIC_VERSION = "Feb 2026"
FHIR_VERSION = "R4"
CDS_HOOKS_SPEC_VERSION = "1.0"
CDS_HOOKS_IMPL_VERSION = "1"

EPIC_SCOPE = (
    "EPIC.FHIR.R4.SERVICES.MedicationRequest.Read "
    "EPIC.FHIR.R4.SERVICES.Condition.Read "
    "EPIC.FHIR.R4.SERVICES.Patient.Read "
    "launch patient/Patient.read patient/Condition.read patient/MedicationRequest.read"
)


def _prefetch() -> dict:
    data = json.loads(_PATIENT_VIEW_FIXTURE.read_text(encoding="utf-8"))
    return data["prefetch"]


def _epic_extension(bpa_trigger_action: int, criteria_id: str) -> dict:
    return {
        "com.epic.cdshooks.request.bpa-trigger-action": bpa_trigger_action,
        "com.epic.cdshooks.request.criteria-id": criteria_id,
        "com.epic.cdshooks.request.epic-version": EPIC_VERSION,
        "com.epic.cdshooks.request.fhir-version": FHIR_VERSION,
        "com.epic.cdshooks.request.cds-hooks-specification-version": CDS_HOOKS_SPEC_VERSION,
        "com.epic.cdshooks.request.cds-hooks-implementation-version": CDS_HOOKS_IMPL_VERSION,
    }


def _fhir_authorization() -> dict:
    return {
        "access_token": "epic-sandbox-token",
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": EPIC_SCOPE,
        "subject": "epicvibe",
    }


def _draft_order_bundle(patient_id: str, draft_med_code: str, draft_med_display: str) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": "draft1",
                    "status": "draft",
                    "intent": "order",
                    "contained": [
                        {
                            "resourceType": "Medication",
                            "id": "medcontained",
                            "code": {
                                "coding": [
                                    {
                                        "system": PREF_LIST_SYSTEM,
                                        "code": draft_med_code,
                                        "display": draft_med_display,
                                    }
                                ]
                            },
                        }
                    ],
                    "medicationReference": {"reference": "#medcontained"},
                    "subject": {"reference": f"Patient/{patient_id}"},
                }
            }
        ],
    }


def epic_patient_view_request(patient_id: str = "PAT1", encounter_id: str = "ENC1",
                              hook_instance: str = "epic-hookinstance-patient-view") -> dict:
    return {
        "hook": "patient-view",
        "hookInstance": hook_instance,
        "fhirServer": "https://example.org/api/FHIR/R4",
        "fhirAuthorization": _fhir_authorization(),
        "context": {
            "userId": "PractitionerRole/PRACTROLE1",
            "patientId": patient_id,
            "encounterId": encounter_id,
        },
        "extension": _epic_extension(60, "criteria-patient-view"),
        "prefetch": _prefetch(),
    }


def epic_order_select_request(patient_id: str = "PAT1", encounter_id: str = "ENC1",
                              hook_instance: str = "epic-hookinstance-order-select",
                              draft_med_code: str = "PREF_MET_500",
                              draft_med_display: str = "metFORMIN 500 mg tablet BID") -> dict:
    return {
        "hook": "order-select",
        "hookInstance": hook_instance,
        "fhirServer": "https://example.org/api/FHIR/R4",
        "fhirAuthorization": _fhir_authorization(),
        "context": {
            "userId": "PractitionerRole/PRACTROLE1",
            "patientId": patient_id,
            "encounterId": encounter_id,
            "selections": ["MedicationRequest/draft1"],
            "draftOrders": _draft_order_bundle(patient_id, draft_med_code, draft_med_display),
        },
        "extension": _epic_extension(18, "criteria-order-select"),
        "prefetch": _prefetch(),
    }


def epic_order_sign_request(patient_id: str = "PAT1", encounter_id: str = "ENC1",
                            hook_instance: str = "epic-hookinstance-order-sign",
                            draft_med_code: str = "PREF_MET_500",
                            draft_med_display: str = "metFORMIN 500 mg tablet BID") -> dict:
    return {
        "hook": "order-sign",
        "hookInstance": hook_instance,
        "fhirServer": "https://example.org/api/FHIR/R4",
        "fhirAuthorization": _fhir_authorization(),
        "context": {
            "userId": "PractitionerRole/PRACTROLE1",
            "patientId": patient_id,
            "encounterId": encounter_id,
            "draftOrders": _draft_order_bundle(patient_id, draft_med_code, draft_med_display),
        },
        "extension": _epic_extension(23, "criteria-order-sign"),
        "prefetch": _prefetch(),
    }
