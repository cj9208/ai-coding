"""运行配置：两个根地址的唯一来源（设计文档 §4.1）。

- ``PHOTO_ROOT``           照片源根（NAS 挂载目录）。默认指向仓库下的占位目录，
  真实值是映射的盘符/UNC——绝对路径只在本文件出现，DB 一律存相对路径。
- ``PHOTO_DESK_DATA_DIR``  产出目录（SQLite + 缩略图缓存），默认 ``data/photo_desk/``，
  后期迁 NAS 只改这个环境变量。

盘符未挂载时 :meth:`Settings.ensure_root` 硬报错，绝不让扫描把"读不到"
误报成"文件没了"（silence-is-a-gap，同 quantdesk 姿势）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils import paths

#: v1 认的照片扩展名（小写，含点）。视频/RAW 明确不在 M0。
IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".heic", ".heif", ".png"})

#: 隔离区目录名，位于 PHOTO_ROOT 之下（同盘才能秒级 rename，D-3）。
BLURRED_DIR_NAME = "blurred"


class PhotoRootError(RuntimeError):
    """照片根目录不可用。访问 NAS 前的一切操作都应先撞这个错，而不是静默扫空。"""


def _default_root() -> Path:
    return paths.data_dir("photo_desk") / "photo_root"


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "photo_desk.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def blurred_dir(self) -> Path:
        return self.root / BLURRED_DIR_NAME

    # -- rel/abs 的唯一转换点 ----------------------------------------------------

    def photo_path(self, rel: str) -> Path:
        """相对路径（POSIX 风格，DB 中的存法）→ 当前配置下的绝对路径。"""
        return self.root / rel

    def rel_of(self, path: Path) -> str:
        """绝对路径 → 相对 root 的 POSIX 风格字符串。root 之外的路径是 bug。"""
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def is_under_blurred(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.blurred_dir.resolve())
        except ValueError:
            return False
        return True

    # -- guards ------------------------------------------------------------------

    def ensure_root(self) -> None:
        if not self.root.is_dir():
            raise PhotoRootError(
                f"照片根目录不可访问: {self.root}\n"
                "若为 NAS 挂载盘，请先确认盘符已映射（PHOTO_ROOT 环境变量可覆盖默认值）。"
                "在挂载恢复之前，本系统拒绝扫描——空结果会被误记成照片消失。"
            )

    def ensure_writable_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)


def load_settings(
    root: str | Path | None = None, data_dir: str | Path | None = None
) -> Settings:
    """CLI 显式参数 > 环境变量 > repo-root 锚定默认值。"""
    resolved_root = Path(
        root if root is not None else os.environ.get("PHOTO_ROOT") or _default_root()
    )
    resolved_data = Path(
        data_dir
        if data_dir is not None
        else os.environ.get("PHOTO_DESK_DATA_DIR") or paths.data_dir("photo_desk")
    )
    return Settings(root=resolved_root, data_dir=resolved_data)
