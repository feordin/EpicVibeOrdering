"""Deterministic, catalog-grounded provider so demos run without an API key.

`DemoProvider` inspects the serialized prompt (the same JSON the real provider
receives: patient summary + catalog shortlist) and returns the canned proposal
for whichever order set the patient's problem list points at.  Anything it can
not parse falls back to `DEMO_PROPOSAL` (the new-T2DM proposal), which keeps the
provider's behaviour stable for callers that pass arbitrary strings.
"""

import json

DEMO_PROPOSAL = {
    "order_sets": [{
        "order_set_id": "AMB_DM2_NEWDX",
        "rationale": "New type 2 diabetes diagnosis without recent A1c or diabetes therapy.",
        "items": [
            {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True,
             "rationale": "Baseline glycemic assessment.",
             "prepopulated": {"priority": "Routine", "specimen": "Blood"},
             "evidence": ["Active problem: type 2 diabetes mellitus",
                          "No A1c result in the summary"]},
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "include": True,
             "rationale": "Baseline cardiovascular risk assessment.",
             "prepopulated": {"priority": "Routine", "specimen": "Blood",
                              "note": "Fasting preferred"},
             "evidence": ["Type 2 diabetes is an ASCVD risk equivalent"]},
            {"item_id": "ITEM_UMALB", "variant_id": "V_UMALB", "include": True,
             "rationale": "Baseline nephropathy screening.",
             "prepopulated": {"priority": "Routine", "specimen": "Urine, random"},
             "evidence": ["Annual albuminuria screening at T2DM diagnosis"]},
            {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True,
             "rationale": "First-line therapy; start low to minimize GI effects.",
             "parameter_recommendations": [{"label": "dose", "value": "500 mg BID",
                                            "rationale": "Titrate after 1-2 weeks"}],
             "prepopulated": {"dose": "500 mg", "route": "oral", "frequency": "BID",
                              "duration": "90 days", "indication": "Type 2 diabetes mellitus"},
             "evidence": ["No diabetes agent on the active medication list"]},
            {"item_id": "ITEM_RETINAL", "variant_id": "V_RETINAL", "include": True,
             "rationale": "Dilated retinal exam within 1 year of T2DM diagnosis.",
             "prepopulated": {"reason": "Diabetic retinopathy screening",
                              "priority": "Routine"},
             "evidence": ["New T2DM diagnosis"]},
        ]}],
    "confidence": "high",
}

CAP_PROPOSAL = {
    "order_sets": [{
        "order_set_id": "ED_CAP_ADMIT",
        "rationale": "Community-acquired pneumonia requiring admission; empiric therapy started "
                     "after cultures, with renal dosing considered.",
        "items": [
            {"item_id": "ITEM_CAP_CBC", "variant_id": "V_CAP_CBC", "include": True,
             "rationale": "Assess leukocytosis and baseline counts.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood"},
             "evidence": ["Acute pneumonia this encounter"]},
            {"item_id": "ITEM_CAP_BMP", "variant_id": "V_CAP_BMP", "include": True,
             "rationale": "Baseline renal function before antibiotic dosing.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood"},
             "evidence": ["Chronic kidney disease on the problem list"]},
            {"item_id": "ITEM_CAP_BCX", "variant_id": "V_CAP_BCX2", "include": True,
             "rationale": "Two sets before the first antibiotic dose.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood",
                              "note": "Draw both sets before first antibiotic dose"},
             "evidence": ["Suspected bacterial pneumonia requiring admission"]},
            {"item_id": "ITEM_CAP_PCT", "variant_id": "V_CAP_PCT", "include": True,
             "rationale": "Supports antibiotic duration decisions.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood"},
             "evidence": ["Bacterial vs viral discrimination in CAP"]},
            {"item_id": "ITEM_CAP_CXR", "variant_id": "V_CAP_CXR_2V", "include": True,
             "rationale": "Confirm consolidation and extent of disease.",
             "prepopulated": {"priority": "STAT", "reason": "Community-acquired pneumonia"},
             "evidence": ["Pneumonia documented this encounter"]},
            {"item_id": "ITEM_CAP_CEFTRIAXONE", "variant_id": "V_CAP_CTX_1G", "include": True,
             "rationale": "Empiric beta-lactam for inpatient CAP.",
             "parameter_recommendations": [{"label": "dose", "value": "1 g IV q24h",
                                            "rationale": "Standard inpatient CAP dosing"}],
             "prepopulated": {"dose": "1 g", "route": "IV", "frequency": "q24h",
                              "duration": "5 days",
                              "indication": "Community-acquired pneumonia"},
             "evidence": ["No beta-lactam allergy recorded"]},
            {"item_id": "ITEM_CAP_AZITHRO", "variant_id": "V_CAP_AZI_IV", "include": True,
             "rationale": "Atypical coverage alongside the beta-lactam.",
             "prepopulated": {"dose": "500 mg", "route": "IV", "frequency": "daily",
                              "duration": "5 days",
                              "indication": "Community-acquired pneumonia"},
             "evidence": ["Guideline-directed combination therapy for admitted CAP"]},
            {"item_id": "ITEM_CAP_SPO2", "variant_id": "V_CAP_SPO2", "include": True,
             "rationale": "Continuous oximetry while hypoxemia risk persists.",
             "prepopulated": {"frequency": "continuous",
                              "note": "Notify provider for SpO2 below 90%"},
             "evidence": ["COPD on the problem list increases hypoxemia risk"]},
            {"item_id": "ITEM_CAP_ISOLATION", "variant_id": "V_CAP_ISO_DROPLET", "include": True,
             "rationale": "Droplet precautions until a viral etiology is excluded.",
             "prepopulated": {"note": "Until respiratory viral panel negative"},
             "evidence": ["Acute respiratory infection on admission"]},
            {"item_id": "ITEM_CAP_LEVOFLOX", "variant_id": "V_CAP_LEVO_750", "include": False,
             "rationale": "Not needed; no beta-lactam allergy, and ceftriaxone is preferred.",
             "evidence": ["No beta-lactam allergy recorded"]},
        ]}],
    "confidence": "high",
}

HF_PROPOSAL = {
    "order_sets": [{
        "order_set_id": "IP_HF_EXACERBATION",
        "rationale": "Acute decompensated heart failure with volume overload; IV diuresis with "
                     "close electrolyte and renal monitoring.",
        "items": [
            {"item_id": "ITEM_HF_BNP", "variant_id": "V_HF_BNP", "include": True,
             "rationale": "Quantify decompensation and trend response.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood"},
             "evidence": ["Elevated BNP on presentation"]},
            {"item_id": "ITEM_HF_BMP", "variant_id": "V_HF_BMP_DAILY", "include": True,
             "rationale": "Daily electrolytes and renal function during IV diuresis.",
             "prepopulated": {"priority": "Routine", "frequency": "daily x3",
                              "specimen": "Blood",
                              "note": "Potassium at the upper limit; recheck with each dose change"},
             "evidence": ["Potassium 5.1 mmol/L", "IV loop diuretic planned"]},
            {"item_id": "ITEM_HF_TROPONIN", "variant_id": "V_HF_TROP", "include": True,
             "rationale": "Exclude ischemic trigger for the exacerbation.",
             "prepopulated": {"priority": "STAT", "specimen": "Blood",
                              "note": "Repeat at 3 hours"},
             "evidence": ["Acute heart failure exacerbation this encounter"]},
            {"item_id": "ITEM_HF_ECG", "variant_id": "V_HF_ECG", "include": True,
             "rationale": "Assess rhythm and ischemia.",
             "prepopulated": {"priority": "STAT"},
             "evidence": ["Atrial fibrillation on the problem list"]},
            {"item_id": "ITEM_HF_ECHO", "variant_id": "V_HF_ECHO", "include": True,
             "rationale": "Reassess ejection fraction and valvular contribution.",
             "prepopulated": {"priority": "Routine", "reason": "Assess LV ejection fraction"},
             "evidence": ["HFrEF on the problem list"]},
            {"item_id": "ITEM_HF_FUROSEMIDE", "variant_id": "V_HF_FURO_40", "include": True,
             "rationale": "IV loop diuresis for volume overload.",
             "parameter_recommendations": [{"label": "dose", "value": "40 mg IV q12h",
                                            "rationale": "Escalate if urine output is inadequate"}],
             "prepopulated": {"dose": "40 mg", "route": "IV", "frequency": "q12h",
                              "indication": "Acute decompensated heart failure"},
             "evidence": ["Volume overload on presentation",
                          "Anticoagulated with apixaban - no dose interaction"]},
            {"item_id": "ITEM_HF_WEIGHTS", "variant_id": "V_HF_WEIGHTS", "include": True,
             "rationale": "Track diuresis response objectively.",
             "prepopulated": {"frequency": "daily", "note": "Before breakfast, same scale"},
             "evidence": ["IV diuresis in progress"]},
            {"item_id": "ITEM_HF_IO", "variant_id": "V_HF_IO", "include": True,
             "rationale": "Strict I&O to titrate diuretics.",
             "prepopulated": {"frequency": "q shift",
                              "note": "Notify provider if urine output below 30 mL/hr"},
             "evidence": ["IV diuresis in progress"]},
            {"item_id": "ITEM_HF_DIET", "variant_id": "V_HF_DIET_2G", "include": True,
             "rationale": "Sodium and fluid restriction during decompensation.",
             "prepopulated": {"note": "2 g sodium; 1.5 L fluid restriction"},
             "evidence": ["Acute decompensated heart failure"]},
            {"item_id": "ITEM_HF_CARDS", "variant_id": "V_HF_CARDS", "include": True,
             "rationale": "Cardiology input on GDMT optimization.",
             "prepopulated": {"priority": "Routine",
                              "reason": "Heart failure exacerbation management"},
             "evidence": ["HFrEF with recurrent decompensation"]},
        ]}],
    "confidence": "high",
}

HTN_PROPOSAL = {
    "order_sets": [{
        "order_set_id": "AMB_HTN",
        "rationale": "Hypertension follow-up; baseline metabolic panel on ACE inhibitor therapy.",
        "items": [
            {"item_id": "ITEM_BMP", "variant_id": "V_BMP", "include": True,
             "rationale": "Monitor potassium and renal function on an ACE inhibitor.",
             "prepopulated": {"priority": "Routine", "specimen": "Blood"},
             "evidence": ["Hypertension on the problem list",
                          "ACE inhibitor on the active medication list"]},
        ]}],
    "confidence": "medium",
}

PROPOSALS = {
    "AMB_DM2_NEWDX": DEMO_PROPOSAL,
    "ED_CAP_ADMIT": CAP_PROPOSAL,
    "IP_HF_EXACERBATION": HF_PROPOSAL,
    "AMB_HTN": HTN_PROPOSAL,
}

# (order_set_id, icd-10 prefixes, free-text keywords) in priority order.
_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("ED_CAP_ADMIT", ("J12", "J13", "J15", "J18"), ("pneumonia", "community-acquired")),
    ("IP_HF_EXACERBATION", ("I50",), ("heart failure", "hfref", "chf", "congestive")),
    ("AMB_DM2_NEWDX", ("E11", "E10"), ("diabetes", "diabetic")),
    ("AMB_HTN", ("I10",), ("hypertension", "blood pressure")),
]


def _haystack(patient: dict) -> tuple[set[str], str]:
    codes, text = set(), []
    for group in ("conditions", "active_orders", "medications"):
        for item in patient.get(group) or []:
            if item.get("code"):
                codes.add(str(item["code"]).upper())
            if item.get("display"):
                text.append(str(item["display"]).lower())
    encounter = patient.get("encounter") or {}
    text.extend(str(r).lower() for r in encounter.get("reasons") or [])
    text.extend(str(t).lower() for t in encounter.get("type") or [])
    return codes, " ".join(text)


def select_proposal(user: str) -> dict:
    """Pick the canned proposal matching the summary+catalog in `user`."""
    try:
        payload = json.loads(user)
        patient = payload.get("patient") or {}
        available = {o.get("order_set_id") for o in payload.get("catalog") or []}
    except Exception:
        return DEMO_PROPOSAL
    if not isinstance(patient, dict):
        return DEMO_PROPOSAL
    codes, text = _haystack(patient)
    for order_set_id, prefixes, keywords in _RULES:
        if available and order_set_id not in available:
            continue
        if any(c.startswith(p) for c in codes for p in prefixes) or \
                any(k in text for k in keywords):
            return PROPOSALS[order_set_id]
    for order_set_id, _, _ in _RULES:
        if order_set_id in available:
            return PROPOSALS[order_set_id]
    return DEMO_PROPOSAL


class DemoProvider:
    """Deterministic catalog-grounded provider for demos without an API key."""

    def describe(self) -> str:
        return "demo"

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        return select_proposal(user)
