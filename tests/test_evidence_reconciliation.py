from coding_review_agent_loop.evidence_reconciliation import (
    MAX_RENDERED_EVIDENCE_ENTRIES,
    reconcile_evidence,
)
from coding_review_agent_loop.protocol import (
    DiscussEvidenceClaim,
    DiscussEvidenceUpdate,
    ParsedDiscussReview,
    parse_structured_discuss_review,
)


def _vote(reviewer, claims=(), updates=()):
    return ParsedDiscussReview(
        outcome="implement", rationale="test", split_proposals=(), reviewer=reviewer,
        evidence_claims=tuple(claims), evidence_updates=tuple(updates),
    )


def test_reconciliation_retracts_old_claim_and_combines_exact_contributors():
    subject = "issue-535"
    first = _vote("Codex", [DiscussEvidenceClaim("Same fact", "reported-but-unverified", "https://x")])
    duplicate = _vote("Gemini", [DiscussEvidenceClaim(" same   fact ", "reported-but-unverified", "https://x")])
    retraction = _vote("Codex", [], [DiscussEvidenceUpdate("retract", "issue-535-r1-Codex-c0", "later inspection disproved it")])
    ledger = reconcile_evidence(subject, [[first, duplicate], [retraction]])
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["contributors"] == ["Gemini"]
    assert ledger["history"][0]["action"] == "retract"


def test_reconciliation_keeps_reported_separate_from_missing_and_bounds_output():
    claims = [DiscussEvidenceClaim(f"fact {index}", "reported-but-unverified") for index in range(60)]
    claims.append(DiscussEvidenceClaim("implementation assertion", "missing"))
    ledger = reconcile_evidence("issue-535", [[_vote("Codex", claims)]])
    assert any(item["status"] == "missing" for item in ledger["entries"])
    assert len(ledger["rendered"]) <= MAX_RENDERED_EVIDENCE_ENTRIES
    assert ledger["omitted_entries"] > 0


def test_protocol_accepts_verified_attestation_and_rejects_missing_citation():
    good = '''{"schema_version":1,"kind":"discuss_review","outcome":"implement","rationale":"r","evidence":{"claims":[{"fact":"checked","status":"verified","source":"src/x.py:4","verification_basis":"checkout-inspected"}],"updates":[]}}
<!-- AGENT_PLAN_STATE: approved -->
-- Codex'''
    parsed = parse_structured_discuss_review(good, reviewer="Codex")
    assert parsed and parsed.evidence_claims[0].status == "verified"
    bad = good.replace('"status":"verified","source":"src/x.py:4","verification_basis":"checkout-inspected"', '"status":"missing","source":"src/x.py:4"')
    try:
        parse_structured_discuss_review(bad, reviewer="Codex")
    except Exception as exc:
        assert "missing evidence claims" in str(exc)
    else:
        raise AssertionError("missing evidence with a citation was accepted")
