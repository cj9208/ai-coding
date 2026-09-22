"""Golden-set harness + CLI surface (no LLM anywhere in this file).

The click commands report through ``CliRunner``, so the assertions read
``result.stdout`` (JSON payloads, query output) and ``result.stderr``
(rejections) instead of capsys.
"""

from __future__ import annotations

import json

from click.testing import CliRunner, Result

from rag import cli
from rag.evaluation import evaluate, load_cases


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


def test_golden_metrics_on_built_store(built, tmp_path):
    store, _ = built
    cases_file = tmp_path / "golden.jsonl"
    cases = [
        {
            "case_id": "leave-01",
            "question": "年假审批",
            "expected_substrings": ["审批"],
            "rationale": "术语精确查找应由词法路命中",
        },
        {
            "case_id": "abstain-01",
            "question": "完全无关的量子纠缠问题",
            "expected_substrings": [],
            "rationale": "无证据必须弃权",
        },
        {
            "case_id": "broken-01",
            "question": "任何东西",
            "expected_substrings": ["这个字符串不存在于语料"],
            "rationale": "空期望必须被大声报告而不是静默通过",
        },
    ]
    cases_file.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8"
    )
    summary = evaluate(store, load_cases(cases_file), k=3)
    assert summary["n_cases"] == 3
    assert summary["hit_rate"] == 1.0
    assert summary["abstention_correct"] == "1/1"
    assert summary["unresolved_case_ids"] == ["broken-01"]


def test_cli_build_status_query_retrieve_only(tmp_path, inbox):
    data = tmp_path / "cli-data"
    result = run("--data-dir", str(data), "build", "--inbox", str(inbox))
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["chunk_count"] > 0

    status = run("--data-dir", str(data), "status")
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["active_version"] == 1

    queried = run("--data-dir", str(data), "query", "年假审批", "--retrieve-only")
    assert queried.exit_code == 0, queried.output
    assert "[1]" in queried.stdout and "审批" in queried.stdout


def test_cli_build_missing_inbox(tmp_path):
    result = run(
        "--data-dir",
        str(tmp_path / "d"),
        "build",
        "--inbox",
        str(tmp_path / "nowhere"),
    )
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_corpus_projection_written(built, tmp_path):
    corpus = tmp_path / "data" / "corpus"
    files = list(corpus.glob("*.md"))
    assert files  # greppable insurance exists
    text = files[0].read_text(encoding="utf-8")
    assert text.strip()
