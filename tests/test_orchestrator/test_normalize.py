"""normalize: deterministic conditioning, Chinese alias table, traceability."""

from orchestrator import normalize as nz


def test_nfc_and_whitespace_collapse() -> None:
    norm = nz.normalize("春晖卡   怎么\t用")
    assert norm.normalized_query == "春晖省钱卡 怎么 用"
    assert norm.candidate_count == 1


def test_alias_hit_is_traceable_and_scored() -> None:
    norm = nz.normalize("春晖卡怎么用")
    assert norm.alias_hits == ["春晖省钱卡"]
    assert norm.rule_hits == ["alias:春晖省钱卡<-春晖卡"]
    # specificity: matched alias 3 chars vs the canonical's longest, 5
    assert norm.top_match_score == 0.6
    assert norm.top2_gap is None


def test_longest_alias_wins_within_one_canonical() -> None:
    norm = nz.normalize("春晖省钱卡怎么续费")
    assert norm.top_match_score == 1.0
    assert norm.rule_hits == ["alias:春晖省钱卡<-春晖省钱卡"]
    # "省钱卡" must not also register as a second derivation
    assert norm.candidate_count == 1


def test_two_canonicals_ranked_with_gap() -> None:
    norm = nz.normalize("春晖卡和报销系统哪个划算")
    assert norm.alias_hits == ["费用报销系统", "春晖省钱卡"]
    assert norm.candidate_count == 2
    assert norm.top_match_score == 1.0  # 报销系统 is its canonical's longest
    assert norm.top2_gap == 0.4


def test_close_candidates_have_small_gap() -> None:
    from orchestrator.assess import CLOSE_TOP2_GAP

    norm = nz.normalize("省钱卡和报销哪个划算")
    # 省钱卡 3/5 = 0.6 vs 报销 2/4 = 0.5 -> a gap the assess pass reads as
    # "close candidates" ambiguity
    assert norm.candidate_count == 2
    assert norm.top2_gap is not None
    assert norm.top2_gap < CLOSE_TOP2_GAP


def test_no_alias_hit_leaves_signals_empty() -> None:
    norm = nz.normalize("今天天气如何")
    assert norm.normalized_query == "今天天气如何"
    assert norm.alias_hits == []
    assert norm.rule_hits == []
    assert norm.top_match_score is None
    assert norm.candidate_count == 0


def test_mixed_language_alias() -> None:
    norm = nz.normalize("HR服务包含哪些")
    assert norm.alias_hits == ["人事服务"]


def test_as_signals_maps_onto_deterministic_signals() -> None:
    from orchestrator.contracts import DeterministicSignals

    norm = nz.normalize("春晖卡怎么用")
    assert DeterministicSignals(**norm.as_signals()).alias_hits == ["春晖省钱卡"]


def test_custom_table_is_honoured() -> None:
    norm = nz.normalize("小蓝盒怎么用", aliases={"打印机": ("小蓝盒",)})
    assert norm.normalized_query == "打印机怎么用"
    assert norm.top_match_score == 1.0
