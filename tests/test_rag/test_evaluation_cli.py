"""检索评估与 CLI 表面回归；生成路径只使用假客户端，不调用真实 API。"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner, Result

from rag import answer as rag_answer
from rag import cli
from rag.contract import Chunk, ChunkType, EvidencePack
from rag.evaluation import evaluate, load_cases
from rag.tracing import load_trace


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


class _CliRawClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat_json(
        self, prompt, system_prompt="", *, schema=None, model=None, temperature=None
    ):
        self.calls += 1
        return deepcopy(self.payload)


def _cli_answer_pack():
    return EvidencePack(
        query="年假怎么休",
        chunks=[
            Chunk(
                chunk_id="c1",
                doc_id="leave.pdf",
                chunk_type=ChunkType.prose,
                is_parent=False,
                parent_chunk_id=None,
                section_path="年假",
                text="年假需要审批。",
                page_span=(0, 0),
                content_hash="h1",
            )
        ],
    )


@pytest.mark.parametrize(
    ("raw_outcome", "claims", "clarification", "expected_outcome", "exit_code"),
    [
        (
            "answered",
            [{"text": "年假需要审批[99]", "refs": [1]}],
            "",
            "answered",
            0,
        ),
        (
            "answered",
            [
                {"text": "年假需要审批[99]", "refs": [1]},
                {"text": "未支撑的额外待遇", "refs": []},
            ],
            "",
            "partial",
            0,
        ),
        ("answered", [], "", "insufficient", 2),
        (
            "answered",
            [{"text": "未支撑的额外待遇", "refs": [99]}],
            "",
            "insufficient",
            2,
        ),
        ("insufficient", [], "", "insufficient", 2),
        ("escalated", [], "", "escalated", 2),
        ("refuse", [], "", "insufficient", 2),
        ("clarify", [], "你想问哪类假？", "clarify", 2),
    ],
)
def test_cli_generated_answer_never_prints_audit_claims(
    tmp_path,
    monkeypatch,
    raw_outcome,
    claims,
    clarification,
    expected_outcome,
    exit_code,
):
    client = _CliRawClient(
        {
            "outcome": raw_outcome,
            "answer": "模型原正文\n\n模型额外断言",
            "claims": claims,
            "clarification": clarification,
        }
    )
    monkeypatch.setattr(rag_answer, "get_client", lambda: client)
    monkeypatch.setattr(cli, "retrieve", lambda *args, **kwargs: _cli_answer_pack())
    assert cli.generate is rag_answer.generate
    data = tmp_path / "answer-data"
    result = run("--data-dir", str(data), "query", "年假怎么休", "--trace")
    assert result.exit_code == exit_code, result.output
    assert client.calls == 1
    assert f"outcome: {expected_outcome}" in result.stdout
    assert "模型" not in result.stdout
    assert "未支撑的额外待遇" not in result.stdout
    assert "[99]" not in result.stdout
    assert "(no citation)" not in result.stdout
    if expected_outcome == "partial":
        assert "证据不完整：" in result.stdout
        assert "年假需要审批 [1]" in result.stdout
    if expected_outcome in ("insufficient", "escalated"):
        assert "证据不足，无法提供有有效引文支持的回答。" in result.stdout
    if expected_outcome == "clarify":
        assert "clarification: 你想问哪类假？" in result.stdout
    trace_files = list((data / "traces").glob("*.jsonl"))
    assert len(trace_files) == 1
    spans = load_trace(trace_files[0])
    completion = next(s for s in spans if s.name == "gen_ai.completion")
    assert completion.attributes["n_claims"] == len(claims)
