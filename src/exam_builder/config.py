"""exam_builder 的路径与常量锚点。

数据/缓存分家遵循仓库 ``data/`` vs ``cache/`` 契约(设计 D-7):
SQLite(含 verbatim 片段)在 ``data/exam_builder/``,源 Word 可能移动,
故片段是资产;可重建的媒体副本在 ``cache/exam_builder/``。
"""

from __future__ import annotations

import os
from pathlib import Path

from utils.paths import cache_dir, data_dir

PROJECT = "exam_builder"


def db_path() -> Path:
    override = os.environ.get("EXAM_DATA_DIR")
    base = Path(override) if override else data_dir(PROJECT)
    return base / "exam.db"


def media_dir() -> Path:
    """可重建的媒体副本(PNG 图 / WMF 公式预览 / OLE bin),按 sha 去重。"""
    return cache_dir(PROJECT) / "media"


def media_path(sha: str, ext: str) -> Path:
    return media_dir() / sha[:2] / f"{sha}.{ext}"
