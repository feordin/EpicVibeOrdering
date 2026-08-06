DEMO_PROPOSAL = {
    "order_sets": [{
        "order_set_id": "AMB_DM2_NEWDX",
        "rationale": "New type 2 diabetes diagnosis without recent A1c or diabetes therapy.",
        "items": [
            {"item_id": "ITEM_A1C", "variant_id": "V_A1C", "include": True,
             "rationale": "Baseline glycemic assessment."},
            {"item_id": "ITEM_LIPID", "variant_id": "V_LIPID", "include": True,
             "rationale": "Baseline cardiovascular risk assessment."},
            {"item_id": "ITEM_UMALB", "variant_id": "V_UMALB", "include": True,
             "rationale": "Baseline nephropathy screening."},
            {"item_id": "ITEM_METFORMIN", "variant_id": "V_MET_500", "include": True,
             "rationale": "First-line therapy; start low to minimize GI effects.",
             "parameter_recommendations": [{"label": "dose", "value": "500 mg BID",
                                            "rationale": "Titrate after 1-2 weeks"}]},
            {"item_id": "ITEM_RETINAL", "variant_id": "V_RETINAL", "include": True,
             "rationale": "Dilated retinal exam within 1 year of T2DM diagnosis."}
        ]}],
    "confidence": "high",
}


class DemoProvider:
    """Deterministic catalog-grounded provider for demos without an API key."""

    async def complete_json(self, *, system: str, user: str, json_schema: dict) -> dict:
        return DEMO_PROPOSAL
