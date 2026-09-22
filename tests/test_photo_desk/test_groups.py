"""golden 驱动的归组测试：规则即契约，改动必须过样例（设计文档 §4.3）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from photo_desk.groups import Groupable, group_bursts

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "photo_groups.jsonl"


def _load(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _to_item(d: dict) -> Groupable:
    raw = d.get("taken_at")
    return Groupable(
        photo_id=d["photo_id"],
        rel_path=d["rel_path"],
        content_hash=d["content_hash"],
        taken_at=datetime.fromisoformat(raw) if raw else None,
        file_mtime=d.get("file_mtime", 0.0),
        burst_id=d.get("burst_id"),
    )


def test_group_golden_cases() -> None:
    for case in _load(GOLDEN):
        items = [_to_item(d) for d in case["items"]]
        groups = group_bursts(items)
        got = sorted(sorted(m.photo_id for m in g.members) for g in groups)
        want = sorted(sorted(ids) for ids in case["expect"])
        assert got == want, f"case {case['case']!r}: got {got}, want {want}"


def test_group_id_is_stable_and_shared() -> None:
    items = [
        _to_item(
            {
                "photo_id": 1,
                "rel_path": "d/IMG_0001.jpg",
                "content_hash": "cafe" + "0" * 60,
                "taken_at": "2025-05-01T09:00:00.000",
            }
        ),
        _to_item(
            {
                "photo_id": 2,
                "rel_path": "d/IMG_0002.jpg",
                "content_hash": "f00d" + "0" * 60,
                "taken_at": "2025-05-01T09:00:00.350",
            }
        ),
    ]
    (g1,) = group_bursts(items)
    (g2,) = group_bursts(list(reversed(items)))
    assert g1.group_id == "cafe00000000"  # 首张（最早）哈希前 12 位
    assert g2.group_id == g1.group_id
    assert g1.level == 2
