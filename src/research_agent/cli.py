"""MVP CLI driver (01 §7). The process may die at AWAIT_USER and a later
`answer` invocation resumes from the DB — that is the point of the design.

    python -m research_agent new --topic "1500 元以内的降噪耳机"
    python -m research_agent answer <session_id>
    python -m research_agent status <session_id>
    python -m research_agent report <session_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid

from .cli_support import build_orchestrator, collect_answers, render_questions
from .config import DATA_DIR, DEFAULT_DB_URL
from .contracts.models import ResearchBrief
from .storage import SessionStore, init_db, make_engine, make_session_factory


def _store() -> SessionStore:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = make_engine(DEFAULT_DB_URL)
    init_db(engine)
    return SessionStore(make_session_factory(engine))


async def _cmd_new(args) -> int:
    store = _store()
    session_id = (
        args.session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    )
    store.create_session(session_id)
    store.append_artifact(
        session_id,
        ResearchBrief(
            session_id=session_id,
            topic=args.topic,
            user_context=args.context or "",
            candidate_domain_hint=args.domain or "",
            language=args.lang,
        ),
    )
    orch = build_orchestrator(store, fake_dir=args.fake, max_collect=args.max_calls)
    print(
        f"session {session_id} — researching… (LLM: 每阶段调用，预算 {args.max_calls} 次采集)"
    )
    phase = await orch.run(session_id)
    return _report_state(store, session_id, phase)


async def _cmd_answer(args) -> int:
    store = _store()
    orch = build_orchestrator(store, fake_dir=args.fake, max_collect=args.max_calls)
    view = orch.load_view(args.session_id)
    clar = view.clarification
    if clar is None or not clar.questions:
        print("当前没有待回答的问题。")
        phase = await orch.run(args.session_id)
        return _report_state(store, args.session_id, phase)
    answers = collect_answers(clar, use_editor=args.editor)
    orch.resume(args.session_id, answers)
    print("已记录回答，继续 DEEPEN→RECOMMEND…")
    phase = await orch.run(args.session_id)
    return _report_state(store, args.session_id, phase)


def _cmd_status(args) -> int:
    store = _store()
    phase = store.get_phase(args.session_id)
    print(
        f"{args.session_id}: {phase}  stop_reason={store.get_stop_reason(args.session_id)}"
    )
    return 0


def _cmd_report(args) -> int:
    store = _store()
    path = store.get_report_path(args.session_id)
    if not path:
        print("尚无报告。", file=sys.stderr)
        return 1
    print(open(path, encoding="utf-8").read())
    return 0


def _report_state(store: SessionStore, session_id: str, phase) -> int:
    phase_name = getattr(phase, "value", str(phase))
    if phase_name == "AWAIT_USER":
        print("\n" + "=" * 60)
        print(render_questions(_load_clarification(store, session_id)))
        print("=" * 60)
        print(f"回答后运行: python -m research_agent answer {session_id}")
        return 0
    path = store.get_report_path(session_id)
    print(f"phase={phase_name}" + (f"\nreport: {path}" if path else ""))
    return 0 if phase_name == "DONE" else 2


def _load_clarification(store: SessionStore, session_id: str):
    from .contracts.models import Clarification

    raw = store.latest_artifact(session_id, "Clarification")
    return Clarification.model_validate(raw) if raw else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    p = argparse.ArgumentParser(prog="research_agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="开一个新会话并跑到需要用户为止")
    n.add_argument("--topic", required=True)
    n.add_argument("--context", default="", help="已知的自身约束，能少问就少问")
    n.add_argument("--domain", default="", help="候选域提示，如 'laptops'")
    n.add_argument("--lang", default="zh")
    n.add_argument("--fake", default="", help="用 fixtures 目录做离线采集")
    n.add_argument("--max-calls", type=int, default=30)
    n.add_argument("--session-id", default="")

    a = sub.add_parser("answer", help="恢复 AWAIT_USER 会话")
    a.add_argument("session_id")
    a.add_argument("--fake", default="")
    a.add_argument("--max-calls", type=int, default=30)
    a.add_argument(
        "--editor", action="store_true", help="在 $EDITOR 风格的自由输入里一次性回答"
    )

    s = sub.add_parser("status")
    s.add_argument("session_id")
    r = sub.add_parser("report")
    r.add_argument("session_id")

    args = p.parse_args(argv)
    if args.cmd == "new":
        return asyncio.run(_cmd_new(args))
    if args.cmd == "answer":
        return asyncio.run(_cmd_answer(args))
    if args.cmd == "status":
        return _cmd_status(args)
    return _cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
