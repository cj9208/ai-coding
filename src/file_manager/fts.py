"""files_fts 虚表的手工同步。

不走触发器，改由服务层在上传/更新/删除时显式调用：
同步逻辑集中、可测试，将来接全文索引时也只动这里。

CJK 折叠：本机 SQLite 的 FTS5 unicode61 分词器会丢弃中日韩字符，
所以写入索引前把 CJK 连续段展开为"单字 + 双字滑窗"并以空格分隔，
查询端做同样的折叠，保证"财报"按相邻双字匹配（而非散落单字的 AND）。
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    filename,
    title,
    notes,
    tags
)
"""

# 常见 CJK 区块：汉字扩展A、汉字、兼容表意、平假名、片假名、谚文音节
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff" "\u3040-\u30ff\uac00-\ud7a3]+"
)


def fold_cjk(text: str | None) -> str:
    """把 CJK 连续段折叠成 单字+双字滑窗，空格分隔；其余字符原样保留。"""

    def fold_run(run: str) -> str:
        units = list(run) + [run[i : i + 2] for i in range(len(run) - 1)]
        return " " + " ".join(units) + " "

    return _CJK_RE.sub(lambda m: fold_run(m.group(0)), text or "")


def init_fts(db: Session) -> None:
    db.execute(text(DDL))
    db.commit()


def fts_insert(
    db: Session,
    file_id: int,
    filename: str,
    title: str | None,
    notes: str,
    tags: str,
) -> None:
    db.execute(
        text(
            "INSERT INTO files_fts (rowid, filename, title, notes, tags) "
            "VALUES (:rowid, :filename, :title, :notes, :tags)"
        ),
        {
            "rowid": file_id,
            "filename": fold_cjk(filename),
            "title": fold_cjk(title),
            "notes": fold_cjk(notes),
            "tags": fold_cjk(tags),
        },
    )


def fts_update(
    db: Session,
    file_id: int,
    filename: str,
    title: str | None,
    notes: str,
    tags: str,
) -> None:
    db.execute(
        text(
            "UPDATE files_fts SET filename=:filename, title=:title, "
            "notes=:notes, tags=:tags WHERE rowid=:rowid"
        ),
        {
            "rowid": file_id,
            "filename": fold_cjk(filename),
            "title": fold_cjk(title),
            "notes": fold_cjk(notes),
            "tags": fold_cjk(tags),
        },
    )


def fts_delete(db: Session, file_id: int) -> None:
    db.execute(text("DELETE FROM files_fts WHERE rowid=:rowid"), {"rowid": file_id})
