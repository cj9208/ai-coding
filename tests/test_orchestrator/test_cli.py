"""CLI smoke: every subcommand against a temp DB, exit codes included.

The click commands are driven through ``CliRunner``, so exit codes come from
``result.exit_code`` and the printed lines from ``result.stdout`` /
``result.stderr``. ``ask`` is stubbed at the ``llm_client.get_client`` seam —
the CLI test stays zero-network like the rest of the suite (live proof is the
M2 milestone).
"""

from pathlib import Path
from typing import Any

from click.testing import CliRunner, Result

from orchestrator import cli
from orchestrator.capabilities.fake import FakeFrontHalf
from orchestrator.registry import load_static
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store

STRONG = {
    "task_type": "test",
    "top_match_score": 0.9,
    "candidate_count": 1,
    "model": {"confidence": 0.9},
}


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "orch.db"
    store = Store(db)
    orch = Orchestrator(store, load_static(), FakeFrontHalf([STRONG]))
    orch.run_turn("查一下余额")
    store.close()
    return db


def test_status_list_and_detail(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    assert run("--db", str(db), "status").exit_code == 0
    store = Store(db)
    rid = store.list_requests()[0]["request_id"]
    store.close()
    assert run("--db", str(db), "status", rid).exit_code == 0
    assert run("--db", str(db), "status", "req_missing").exit_code == 1


def test_replay_prints_decision_path(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    store = Store(db)
    rid = store.list_requests()[0]["request_id"]
    store.close()
    result = run("--db", str(db), "replay", rid)
    assert result.exit_code == 0
    out = result.stdout
    assert "routing" in out and "execution" in out and "outcome" in out
    # 05d step 3: replay names the config the request was processed under
    # (the seeded request used the static fixture -> honest "unrecorded")
    assert "config: unrecorded" in out and "(now:" in out


def test_handoff_empty_then_export(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    assert run("--db", str(db), "handoff", "list").exit_code == 0
    assert run("--db", str(db), "handoff", "export", "handoff_nope").exit_code == 1


def test_handoff_worklist_claim_resolve_lifecycle(tmp_path: Path) -> None:
    from orchestrator.capabilities.builtin import build_handoff_packet
    from orchestrator.contracts import RequestEnvelope

    db = tmp_path / "wl.db"
    store = Store(db)
    env = RequestEnvelope.new(text="需要人工的问题", user_id="u1")
    store.create_request(env)
    packet = build_handoff_packet(
        env, reason_code="test", reason_summary="r", objects=[]
    )
    store.append_object(env.request_id, "handoff", packet, env.timestamp_start_ms)
    store.close()
    hid = packet.handoff_id

    listed = run("--db", str(db), "handoff", "worklist")
    assert listed.exit_code == 0
    assert hid in listed.stdout and "open" in listed.stdout

    # resolve before claim is refused; claim then resolve closes it
    assert (
        run(
            "--db",
            str(db),
            "handoff",
            "resolve",
            hid,
            "--assignee",
            "ops_a",
            "--note",
            "x",
        ).exit_code
        == 1
    )
    claimed = run("--db", str(db), "handoff", "claim", hid, "--assignee", "ops_a")
    assert claimed.exit_code == 0
    assert "claimed" in claimed.stdout
    taken = run("--db", str(db), "handoff", "claim", hid, "--assignee", "ops_b")
    assert taken.exit_code == 1
    assert (
        run(
            "--db",
            str(db),
            "handoff",
            "claim",
            hid,
            "--assignee",
            "ops_b",
            "--reassign",
        ).exit_code
        == 0
    )
    assert (
        run(
            "--db",
            str(db),
            "handoff",
            "resolve",
            hid,
            "--assignee",
            "ops_b",
            "--note",
            "已处理",
        ).exit_code
        == 0
    )

    # resolved hides by default, shows with --all
    hidden = run("--db", str(db), "handoff", "worklist")
    assert hidden.exit_code == 0
    assert hid not in hidden.stdout
    shown = run("--db", str(db), "handoff", "worklist", "--all")
    assert shown.exit_code == 0
    assert hid in shown.stdout


def test_golden_run_on_default_file() -> None:
    assert run("golden", "run").exit_code == 0


def test_status_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    assert run("--db", str(db), "status").exit_code == 0
    Store(db).close()


# -- ask (M1 front half, stubbed at the llm_client seam) ----------------------
class _StubClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.n = 0

    async def chat_json(self, prompt, system_prompt="", *, schema=None, **kw):
        payload = self.payloads[min(self.n, len(self.payloads) - 1)]
        self.n += 1
        return schema.model_validate(payload)


def _patch_ask_deps(monkeypatch, payloads: list[dict[str, Any]]) -> _StubClient:
    """ask's two seams: the front half's llm_client, and the production
    registry (pinned back to the echo fixture so CLI tests never need a
    built rag corpus)."""
    import llm_client
    import orchestrator.registry as registry_mod

    stub = _StubClient(payloads)
    monkeypatch.setattr(llm_client, "get_client", lambda **kw: stub)
    monkeypatch.setattr(registry_mod, "load_default", registry_mod.load_static)
    return stub


def test_ask_completed(tmp_path: Path, monkeypatch) -> None:
    _patch_ask_deps(monkeypatch, [{"task_type": "faq_howto", "confidence": 0.9}])
    result = run("--db", str(tmp_path / "ask.db"), "ask", "春晖省钱卡怎么续费")
    assert result.exit_code == 0, result.output
    assert "春晖省钱卡怎么续费" in result.stdout and "[completed]" in result.stdout


def test_ask_clarify_then_resume(tmp_path: Path, monkeypatch) -> None:
    _patch_ask_deps(
        monkeypatch,
        [
            {
                "task_type": "faq_howto",
                "confidence": 0.2,
                "user_resolvable_ambiguity": True,
                "clarification_question": "您指的是哪张卡？",
            },
            {"task_type": "faq_howto", "confidence": 0.9},
        ],
    )
    db = tmp_path / "ask2.db"
    first = run("--db", str(db), "ask", "这张卡怎么续费")
    assert first.exit_code == 0, first.output
    out = first.stdout
    assert "需要澄清：您指的是哪张卡？" in out and "[awaiting_clarification]" in out
    rid = out.rsplit("request_id=", 1)[1].strip()
    second = run("--db", str(db), "ask", "春晖省钱卡", "--resume", rid)
    assert second.exit_code == 0
    assert "[completed]" in second.stdout


def test_ask_front_half_failure_exits_1(tmp_path: Path, monkeypatch) -> None:
    import llm_client

    class _Boom:
        async def chat_json(self, *a, **kw):
            raise RuntimeError("provider down")

    monkeypatch.setattr(llm_client, "get_client", lambda **kw: _Boom())
    result = run("--db", str(tmp_path / "ask3.db"), "ask", "随便问点什么")
    assert result.exit_code == 1
    assert "ask failed" in result.stderr
