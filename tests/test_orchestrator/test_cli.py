"""CLI smoke: every subcommand against a temp DB, exit codes included.

``ask`` is stubbed at the ``llm_client.get_client`` seam — the CLI test stays
zero-network like the rest of the suite (live proof is the M2 milestone).
"""

from pathlib import Path
from typing import Any

from orchestrator.capabilities.fake import FakeFrontHalf
from orchestrator.cli import main
from orchestrator.registry import load_static
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store

STRONG = {
    "task_type": "test",
    "top_match_score": 0.9,
    "candidate_count": 1,
    "model": {"confidence": 0.9},
}


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "orch.db"
    store = Store(db)
    orch = Orchestrator(store, load_static(), FakeFrontHalf([STRONG]))
    orch.run_turn("查一下余额")
    store.close()
    return db


def test_status_list_and_detail(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    assert main(["--db", str(db), "status"]) == 0
    store = Store(db)
    rid = store.list_requests()[0]["request_id"]
    store.close()
    assert main(["--db", str(db), "status", rid]) == 0
    assert main(["--db", str(db), "status", "req_missing"]) == 1


def test_replay_prints_decision_path(tmp_path: Path, capsys) -> None:
    db = _seed(tmp_path)
    store = Store(db)
    rid = store.list_requests()[0]["request_id"]
    store.close()
    assert main(["--db", str(db), "replay", rid]) == 0
    out = capsys.readouterr().out
    assert "routing" in out and "execution" in out and "outcome" in out


def test_handoff_empty_then_export(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    assert main(["--db", str(db), "handoff", "list"]) == 0
    assert main(["--db", str(db), "handoff", "export", "handoff_nope"]) == 1


def test_handoff_worklist_claim_resolve_lifecycle(tmp_path: Path, capsys) -> None:
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

    assert main(["--db", str(db), "handoff", "worklist"]) == 0
    out = capsys.readouterr().out
    assert hid in out and "open" in out

    # resolve before claim is refused; claim then resolve closes it
    assert (
        main(
            [
                "--db",
                str(db),
                "handoff",
                "resolve",
                hid,
                "--assignee",
                "ops_a",
                "--note",
                "x",
            ]
        )
        == 1
    )
    assert main(["--db", str(db), "handoff", "claim", hid, "--assignee", "ops_a"]) == 0
    assert "claimed" in capsys.readouterr().out
    assert main(["--db", str(db), "handoff", "claim", hid, "--assignee", "ops_b"]) == 1
    assert (
        main(
            [
                "--db",
                str(db),
                "handoff",
                "claim",
                hid,
                "--assignee",
                "ops_b",
                "--reassign",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--db",
                str(db),
                "handoff",
                "resolve",
                hid,
                "--assignee",
                "ops_b",
                "--note",
                "已处理",
            ]
        )
        == 0
    )

    # resolved hides by default, shows with --all
    capsys.readouterr()  # drain the claim/resolve echoes
    assert main(["--db", str(db), "handoff", "worklist"]) == 0
    assert hid not in capsys.readouterr().out
    assert main(["--db", str(db), "handoff", "worklist", "--all"]) == 0
    assert hid in capsys.readouterr().out


def test_golden_run_on_default_file() -> None:
    assert main(["golden", "run"]) == 0


def test_status_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    assert main(["--db", str(db), "status"]) == 0
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


def test_ask_completed(tmp_path: Path, monkeypatch, capsys) -> None:
    _patch_ask_deps(monkeypatch, [{"task_type": "faq_howto", "confidence": 0.9}])
    rc = main(["--db", str(tmp_path / "ask.db"), "ask", "春晖省钱卡怎么续费"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "春晖省钱卡怎么续费" in out and "[completed]" in out


def test_ask_clarify_then_resume(tmp_path: Path, monkeypatch, capsys) -> None:
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
    assert main(["--db", str(db), "ask", "这张卡怎么续费"]) == 0
    out = capsys.readouterr().out
    assert "需要澄清：您指的是哪张卡？" in out and "[awaiting_clarification]" in out
    rid = out.rsplit("request_id=", 1)[1].strip()
    assert main(["--db", str(db), "ask", "春晖省钱卡", "--resume", rid]) == 0
    assert "[completed]" in capsys.readouterr().out


def test_ask_front_half_failure_exits_1(tmp_path: Path, monkeypatch, capsys) -> None:
    import llm_client

    class _Boom:
        async def chat_json(self, *a, **kw):
            raise RuntimeError("provider down")

    monkeypatch.setattr(llm_client, "get_client", lambda **kw: _Boom())
    rc = main(["--db", str(tmp_path / "ask3.db"), "ask", "随便问点什么"])
    assert rc == 1
    assert "ask failed" in capsys.readouterr().err
