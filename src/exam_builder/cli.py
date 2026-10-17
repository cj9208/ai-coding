"""``exam`` CLI — 题库入库与检索的入口(M0,无 UI)。

    exam ingest <dir>                 # 扫目录,切题入库(幂等)
    exam ls [--errors]                # 文档清单 / 只看入库失败
    exam search <关键词>… [--chapter 13] [--section 13.2] [--lesson 第1课时]

设计见 docs/exam-builder-design.md。Web UI 与组卷在 M1 加 ``serve``/``compose``。
"""

from __future__ import annotations

from pathlib import Path

import click

from . import ingest as ingest_mod
from .store import Document, Question, Store


@click.group()
def cli() -> None:
    """本地数学题库:入库 / 检索 / 组卷。"""


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--force", is_flag=True, help="忽略 sha 未变,强制重切(解析规则修正后重建)"
)
def ingest(directory: str, force: bool) -> None:
    """扫描 DIRECTORY 下的 .docx/.doc,切题入库(幂等,sha 未变跳过)。"""
    store = Store()
    report = ingest_mod.ingest_dir(store, Path(directory), force=force)
    store.dispose()
    for d in report.docs:
        flag = "ok " if d.status == "ok" else "ERR"
        extra = f"  {d.error}" if d.error else ""
        click.echo(f"[{flag}] {d.n_questions:>3} 题  {d.filename}{extra}")
    click.echo(
        f"\n{report.n_ok} ok / {report.n_error} error, "
        f"{report.n_questions} questions total"
    )


@cli.command()
@click.option("--errors", is_flag=True, help="只列入库失败的文档")
def ls(errors: bool) -> None:
    """文档清单:章节/课时/题数/状态。"""
    store = Store()
    with store.client.session() as db:
        query = db.query(Document)
        if errors:
            query = query.filter(Document.status != "ok")
        docs = query.order_by(Document.filename).all()
        for doc in docs:
            n = db.query(Question.id).filter(Question.doc_id == doc.id).count()
            tag = f"{doc.section or doc.chapter}"
            if doc.lesson:
                tag += f" {doc.lesson}"
            click.echo(f"{doc.status:>5}  {n:>3} 题  [{tag}] {doc.title}")
            if doc.error_msg:
                click.echo(f"        └─ {doc.error_msg}")
    store.dispose()


@cli.command()
@click.argument("keywords", nargs=-1, required=True)
@click.option("--chapter", default="", help="章,如 13")
@click.option("--section", default="", help="节,如 13.2.1")
@click.option("--lesson", default="", help="课时,如 第1课时")
@click.option("--limit", default=50, show_default=True)
def search(
    keywords: tuple, chapter: str, section: str, lesson: str, limit: int
) -> None:
    """按关键词(FTS5+CJK 折叠)+ 章节筛选检索题目。"""
    store = Store()
    with store.client.session() as db:
        hits = store.search(
            db,
            q=" ".join(keywords),
            chapter=chapter,
            section=section,
            lesson=lesson,
            limit=limit,
        )
        for q in hits:
            doc = db.get(Document, q.doc_id)
            if doc is None:
                continue
            badges = ""
            if q.n_figure:
                badges += f"〔图×{q.n_figure}〕"
            if q.n_formula:
                badges += f"〔公式×{q.n_formula}〕"
            first = q.stem_text.splitlines()[0] if q.stem_text else ""
            click.echo(f"#{q.id} [{doc.section or doc.chapter}] {first} {badges}")
    store.dispose()


@cli.command()
@click.option("--ids", required=True, help="逗号分隔的题 id,如 4,8,63")
@click.option("--title", default="", help="试卷标题")
@click.option(
    "--out",
    default="paper.docx",
    show_default=True,
    type=click.Path(dir_okay=False),
)
def compose(ids: str, title: str, out: str) -> None:
    """把选中的题拼成一份新 .docx(公式/图原样保真)。"""
    from . import compose as compose_mod

    qids = [int(x) for x in ids.split(",") if x.strip()]
    store = Store()
    path = compose_mod.compose(store, qids, title, Path(out))
    store.dispose()
    click.echo(f"wrote {path}")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
def serve(host: str, port: int) -> None:
    """起 Web UI(浏览/搜索/组卷)。"""
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    cli()
