"""增量清点：mtime+size 先筛、sha256 后算（quantdesk diff-sync 姿势）。

对 ``PHOTO_ROOT`` 只读；``blurred/`` 整目录不参与遍历（M1 的账本负责其状态）。
网络盘"连得上但没数据"按缺口处理：入口先 ``ensure_root()`` 硬报错。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BLURRED_DIR_NAME, IMAGE_EXTS, Settings
from .exif import read_exif
from .library import STATE_IN_PLACE, STATE_MISSING, Photo

#: SMB 上 stat 便宜、读文件贵——命中 (size, mtime_ns) 即跳过是全部性能设计的前提。
_CHUNK = 1024 * 1024
_PROGRESS_EVERY = 100
_COMMIT_EVERY = 200


@dataclass
class ScanStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    recovered: int = 0  # missing → in_place（重新出现）
    missing: int = 0  # in_place → missing（目录里没了）

    @property
    def total(self) -> int:
        return self.new + self.updated + self.unchanged


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def iter_photo_files(root: Path) -> Iterator[Path]:
    """遍历照片文件，跳过隔离区目录本身。排序稳定，进度可核对。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        if BLURRED_DIR_NAME in path.relative_to(root).parts:
            continue
        yield path


def scan(settings: Settings, session: Session) -> ScanStats:
    """一次全树增量清点。幂等：无变化时二次运行只产生 unchanged。"""
    settings.ensure_root()
    stats = ScanStats()
    indexed: dict[str, Photo] = {
        photo.rel_path: photo for photo in session.scalars(select(Photo))
    }
    seen: set[str] = set()
    dirty = 0

    for path in iter_photo_files(settings.root):
        rel = path.relative_to(settings.root).as_posix()
        seen.add(rel)
        row = indexed.get(rel)
        try:
            st = path.stat()
        except OSError:
            # SMB 抖动：按"没看到"处理，本轮不动它，下轮再说
            seen.discard(rel)
            continue

        if (
            row is not None
            and row.size == st.st_size
            and row.file_mtime_ns == st.st_mtime_ns
        ):
            if row.state == STATE_MISSING:
                row.state = STATE_IN_PLACE
                stats.recovered += 1
                dirty += 1
            else:
                stats.unchanged += 1
            continue

        digest = sha256_file(path)
        if row is not None and row.content_hash == digest:
            # 内容未变（如被复制/回写导致 mtime 变化）：只刷新指纹字段
            row.file_mtime_ns = st.st_mtime_ns
            row.state = STATE_IN_PLACE
            stats.updated += 1
        else:
            meta = read_exif(path)
            if row is None:
                row = Photo(rel_path=rel)
                session.add(row)
                stats.new += 1
            else:
                stats.updated += 1
            row.content_hash = digest
            row.size = st.st_size
            row.file_mtime_ns = st.st_mtime_ns
            row.extension = path.suffix.lower().lstrip(".")
            row.state = STATE_IN_PLACE
            row.taken_at = meta.taken_at
            row.gps_lat = meta.gps_lat
            row.gps_lng = meta.gps_lng
            row.make = meta.make
            row.model = meta.model
            row.width = meta.width
            row.height = meta.height
        dirty += 1

        done = stats.new + stats.updated + stats.unchanged
        if done % _PROGRESS_EVERY == 0:
            print(f"scan: {done} files (new={stats.new} updated={stats.updated})")
        if dirty >= _COMMIT_EVERY:
            session.commit()
            dirty = 0

    for rel, row in indexed.items():
        if rel not in seen and row.state == STATE_IN_PLACE:
            row.state = STATE_MISSING
            stats.missing += 1
            dirty += 1

    if dirty:
        session.commit()
    return stats
