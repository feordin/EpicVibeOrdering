from epicvibe.audit.store import AuditStore
from epicvibe.proposal.schema import Proposal
from epicvibe.proposal.validation import ValidatedProposal


def test_roundtrip():
    store = AuditStore(":memory:")
    vp = ValidatedProposal(proposal=Proposal(order_sets=[], confidence="low"))
    store.record_proposal("ENC1", vp, model="claude-haiku-4-5-20251001")
    rows = store.proposals()
    assert rows[0]["encounter_key"] == "ENC1" and "low" in rows[0]["proposal_json"]

    n = store.record_feedback("epicvibe-order-select",
        {"feedback": [{"card": "uuid-1", "outcome": "accepted", "outcomeTimestamp": "2026-08-04T10:00:00Z"}]})
    assert n == 1
    assert store.feedback()[0]["outcome"] == "accepted"
