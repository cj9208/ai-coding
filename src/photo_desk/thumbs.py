"""缩略图/预览图的按需派生与缓存（设计文档 §8.2 的 M0 落点）。

策略（M0 定）：两档尺寸——列表 512、详情 1600；一律经 Pillow 解码后重采样
存 WebP 到 ``<DATA_DIR>/thumbs/``，缓存键用内容哈希（原件换内容则缓存自然失效）。
详情也走派生图而非原件本身：HEIC 浏览器不认，且避免把 SMB 全尺寸读进每次浏览。
派生只读原件、不回写（D-5）。
"""

from __future__ import annotations

from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from .config import Settings
from .library import Photo

pillow_heif.register_heif_opener()

_SIZES = {"thumb": 512, "preview": 1600}
_WEBP_QUALITY = 80


def cache_path(settings: Settings, photo: Photo, kind: str) -> Path:
    long_edge = _SIZES[kind]
    return settings.thumbs_dir / f"{photo.content_hash[:12]}_{long_edge}.webp"


def ensure_image(settings: Settings, photo: Photo, kind: str) -> Path | None:
    """返回缓存好的派生图路径；解码失败返回 None（UI 显示占位而非 500）。"""
    dest = cache_path(settings, photo, kind)
    if dest.exists():
        return dest
    source = settings.photo_path(photo.rel_path)
    if not source.is_file():
        return None
    try:
        with Image.open(source) as opened:
            img = ImageOps.exif_transpose(opened)
            img.thumbnail((_SIZES[kind], _SIZES[kind]))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            tmp = dest.with_suffix(".part")
            img.save(tmp, "WEBP", quality=_WEBP_QUALITY)
    except Exception:  # noqa: BLE001 - 坏文件不该让页面崩
        return None
    tmp.replace(dest)
    return dest
