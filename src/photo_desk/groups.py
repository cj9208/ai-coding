"""连拍归组：三级兜底信号（设计文档 §4.3）。

输入是从库里读出的轻量记录（Groupable），输出带 `group_id` 的组——
**规则改动必须过 golden 样例** `tests/golden/photo_groups.jsonl`，
防止"重新解释历史"的静默漂移。

1. `burst_id`：苹果 BurstIdentifier（XMP/maker-note），权威但需真机样例核实
   读取路径（TODO.md 欠账）——现在恒为 None，不阻塞 2/3 级；
2. EXIF 毫秒时刻连续（同目录同干、相邻间隔 < 1s）；
3. 无拍摄时刻时的文件名兜底（同目录同干、序号相邻、mtime 相近）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

#: 相邻两连拍的秒级间隔上限（超过即视为两组）。
_BURST_GAP = 1.0
#: 文件名兜底：序号最大跳变、mtime 最大间距（秒）。
_MAX_SEQ_JUMP = 5
_MAX_MTIME_JUMP = 30.0

_TRAILING_DIGITS = re.compile(r"\d+$")


@dataclass
class Groupable:
    photo_id: int
    rel_path: str
    content_hash: str
    taken_at: datetime | None = None
    file_mtime: float = 0.0
    burst_id: str | None = None


@dataclass
class BurstGroup:
    group_id: str
    level: int  # 命中的信号级（1/2/3），入证据便于复盘
    members: list[Groupable] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)


def stem_of(rel_path: str) -> tuple[str, str]:
    """文件名去掉扩展与尾号：'a/IMG_0003.jpg' -> ('a', 'IMG_')。"""
    p = PurePosixPath(rel_path)
    return str(p.parent), _TRAILING_DIGITS.sub("", p.stem)


def _seq_of(rel_path: str) -> int | None:
    p = PurePosixPath(rel_path)
    m = _TRAILING_DIGITS.search(p.stem)
    return int(m.group()) if m else None


def group_id_of(head: Groupable) -> str:
    # 组 id：首张（时间最早）内容哈希前 12 位，重跑稳定（哈希不变则组不变）。
    return head.content_hash[:12]


def group_bursts(items: list[Groupable]) -> list[BurstGroup]:
    """把照片划进连拍组；未入任何组的照片不出现在结果里。"""
    groups: list[BurstGroup] = []
    claimed: set[int] = set()

    # —— 一级：burst_id ——
    by_id: dict[tuple[str, str], list[Groupable]] = {}
    for it in items:
        if it.burst_id:
            key = (str(PurePosixPath(it.rel_path).parent), it.burst_id)
            by_id.setdefault(key, []).append(it)
    for members in by_id.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m.taken_at or datetime.min, m.rel_path))
        claimed.update(m.photo_id for m in members)
        groups.append(BurstGroup(group_id_of(members[0]), 1, members))

    # —— 二级：毫秒时刻连续 ——
    by_stem: dict[tuple[str, str], list[Groupable]] = {}
    for it in items:
        if it.photo_id in claimed or it.taken_at is None:
            continue
        by_stem.setdefault(stem_of(it.rel_path), []).append(it)
    for members in by_stem.values():
        members.sort(key=lambda m: (m.taken_at, m.rel_path))  # type: ignore[arg-type]
        run = [members[0]]
        for nxt in members[1:]:
            gap = (nxt.taken_at - run[-1].taken_at).total_seconds()  # type: ignore[operator]
            if 0 <= gap < _BURST_GAP:
                run.append(nxt)
            else:
                _emit(run, 2, groups, claimed)
                run = [nxt]
        _emit(run, 2, groups, claimed)

    # —— 三级：文件名序号 + mtime（**仅限无 EXIF 时间**的残兵：二级按时间
    # 判过不算连拍的，不许靠文件名相邻翻案）——
    left = [it for it in items if it.photo_id not in claimed and it.taken_at is None]
    by_stem3: dict[tuple[str, str], list[Groupable]] = {}
    for it in left:
        parent, stem = stem_of(it.rel_path)
        if _seq_of(it.rel_path) is not None and stem:
            by_stem3.setdefault((parent, stem), []).append(it)
    for members in by_stem3.values():
        members.sort(key=lambda m: (_seq_of(m.rel_path) or 0, m.file_mtime))
        run = [members[0]]
        for nxt in members[1:]:
            jump = (_seq_of(nxt.rel_path) or 0) - (_seq_of(run[-1].rel_path) or 0)
            mt = abs(nxt.file_mtime - run[-1].file_mtime)
            if 1 <= jump <= _MAX_SEQ_JUMP and mt <= _MAX_MTIME_JUMP:
                run.append(nxt)
            else:
                _emit(run, 3, groups, claimed)
                run = [nxt]
        _emit(run, 3, groups, claimed)

    return groups


def _emit(
    run: list[Groupable],
    level: int,
    groups: list[BurstGroup],
    claimed: set[int],
) -> None:
    if len(run) >= 2 and not any(m.photo_id in claimed for m in run):
        groups.append(BurstGroup(group_id_of(run[0]), level, list(run)))
        claimed.update(m.photo_id for m in run)
