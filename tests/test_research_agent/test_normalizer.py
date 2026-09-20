from __future__ import annotations

from research_agent.research.normalizer import (
    SupportClusterer,
    quote_ok,
)

CAPTURE = (
    "The Model X costs 3299 yuan with 16GB RAM.\n"
    "Battery   life\nreaches 14 hours. Owners complain about heat."
)


class TestQuoteAudit:
    def test_exact_span_ok(self):
        assert quote_ok("The Model X costs 3299 yuan with 16GB RAM.", CAPTURE)

    def test_whitespace_noise_ok(self):
        # re-wrapped lines are legitimate; paraphrase is not
        assert quote_ok("Battery life reaches 14 hours.", CAPTURE)

    def test_paraphrase_rejected(self):
        assert not quote_ok(
            "The Model X is very affordable at 3299 yuan " "plus taxes and a bonus.",
            CAPTURE,
        )

    def test_empty_rejected(self):
        assert not quote_ok("", CAPTURE)


class TestSupportClustering:
    def test_near_dupes_share_key(self):
        c = SupportClusterer()
        k1 = c.key_for("the battery life reaches 14 hours on the model x")
        k2 = c.key_for("Battery life reaches 14 hours on Model X!!!")
        assert k1 == k2

    def test_different_claims_split(self):
        c = SupportClusterer()
        k1 = c.key_for("Model X costs 3299 yuan")
        k2 = c.key_for("Model Y ships with only 8GB RAM")
        assert k1 != k2

    def test_restore_from_persisted(self):
        c = SupportClusterer()
        k1 = c.key_for("Model X costs 3299 yuan")
        from research_agent.research.normalizer import claim_tokens

        d = SupportClusterer()
        d.restore_from({k1: claim_tokens("Model X costs 3299 yuan")})
        assert d.key_for("Model X costs 3299 yuan!!!") == k1
