"""files_fts 索引的域定义：表名 + 被索引列。

机制（fold_cjk 折叠、建表 DDL、upsert/delete 同步）全部在通用层
``storage.fts.FtsTable``；本模块只声明"文件域索引哪些列"，以及把
FileMeta 映射成索引字典的小助手。

同步仍走显式调用（服务层在上传/更新/删除时调 FILES_INDEX），不用触发器：
逻辑集中、可测试，将来换非 SQLite 索引也只动这里。
"""

from __future__ import annotations

from storage import FtsTable

from .models import FileMeta

#: (可读标签, FTS 列名)；顺序与 metadata 搜索后端的 _FIELDS 对应
INDEXED_COLUMNS = ("filename", "title", "notes", "tags")

FILES_INDEX = FtsTable("files_fts", INDEXED_COLUMNS)


def fts_values(meta: FileMeta) -> dict[str, str | None]:
    """从 ORM 行提取索引列的字典（写端折叠由 FtsTable 内部完成）。"""
    return {
        "filename": meta.original_filename,
        "title": meta.title,
        "notes": meta.notes,
        "tags": meta.tags,
    }
