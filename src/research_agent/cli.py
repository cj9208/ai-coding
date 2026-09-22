"""MVP CLI driver (01 §7). The process may die at AWAIT_USER and a later
`answer` invocation resumes from the DB — that is the point of the design.

    python -m research_agent new --topic "1500 元以内的降噪耳机"
    python -m research_agent answer <session_id>
    python -m research_agent status <session_id>
    python -m research_agent report <session_id>

Declaration notes (post-migration from argparse, repo-wide click
convention): only the declaration layer moved. ``new``/``answer`` still run
their coroutines through ``asyncio.run`` inside the command body, so the
async internals are untouched, and the int the old ``main(argv)`` returned
is now the command's exit status via ``raise SystemExit(...)``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid

import click

from .cli_support import build_orchestrator, collect_answers, render_questions
from .config import DATA_DIR, DEFAULT_DB_URL
from .contracts.models import ResearchBrief
from .persistence import SessionStore


def _store() -> SessionStore:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return SessionStore.open(DEFAULT_DB_URL)


async def _cmd_new(
    topic: str,
    context: str,
    domain: str,
    lang: str,
    fake: str,
    max_calls: int,
    session_id: str,
) -> int:
    store = _store()
    session_id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    store.create_session(session_id)
    store.append_artifact(
        session_id,
        ResearchBrief(
            session_id=session_id,
            topic=topic,
            user_context=context,
            candidate_domain_hint=domain,
            language=lang,
        ),
    )
    orch = build_orchestrator(store, fake_dir=fake, max_collect=max_calls)
    print(
        f"session {session_id} — researching… (LLM: 每阶段调用，预算 {max_calls} 次采集)"
    )
    phase = await orch.run(session_id)
    return _report_state(store, session_id, phase)


async def _cmd_answer(session_id: str, fake: str, max_calls: int, editor: bool) -> int:
    store = _store()
    orch = build_orchestrator(store, fake_dir=fake, max_collect=max_calls)
    view = orch.load_view(session_id)
    clar = view.clarification
    if clar is None or not clar.questions:
        print("当前没有待回答的问题。")
        phase = await orch.run(session_id)
        return _report_state(store, session_id, phase)
    answers = collect_answers(clar, use_editor=editor)
    orch.resume(session_id, answers)
    print("已记录回答，继续 DEEPEN→RECOMMEND…")
    phase = await orch.run(session_id)
    return _report_state(store, session_id, phase)


def _cmd_status(session_id: str) -> int:
    store = _store()
    phase = store.get_phase(session_id)
    print(f"{session_id}: {phase}  stop_reason={store.get_stop_reason(session_id)}")
    return 0


def _cmd_report(session_id: str) -> int:
    store = _store()
    path = store.get_report_path(session_id)
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


@click.group()
def cli() -> None:
    """research & recommendation agent: new / answer / status / report"""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )


@cli.command()
@click.option("--topic", required=True)
@click.option("--context", default="", help="已知的自身约束，能少问就少问")
@click.option("--domain", default="", help="候选域提示，如 'laptops'")
@click.option("--lang", default="zh")
@click.option("--fake", default="", help="用 fixtures 目录做离线采集")
@click.option("--max-calls", type=int, default=30)
@click.option("--session-id", default="")
def new(
    topic: str,
    context: str,
    domain: str,
    lang: str,
    fake: str,
    max_calls: int,
    session_id: str,
) -> None:
    """开一个新会话并跑到需要用户为止"""
    raise SystemExit(
        asyncio.run(_cmd_new(topic, context, domain, lang, fake, max_calls, session_id))
    )


@cli.command()
@click.argument("session_id")
@click.option("--fake", default="")
@click.option("--max-calls", type=int, default=30)
@click.option("--editor", is_flag=True, help="在 $EDITOR 风格的自由输入里一次性回答")
def answer(session_id: str, fake: str, max_calls: int, editor: bool) -> None:
    """恢复 AWAIT_USER 会话"""
    raise SystemExit(asyncio.run(_cmd_answer(session_id, fake, max_calls, editor)))


@cli.command()
@click.argument("session_id")
def status(session_id: str) -> None:
    raise SystemExit(_cmd_status(session_id))


@cli.command()
@click.argument("session_id")
def report(session_id: str) -> None:
    raise SystemExit(_cmd_report(session_id))


if __name__ == "__main__":
    cli()
