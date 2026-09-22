"""EXIF 提取：拍摄时刻（含亚秒）、GPS、设备、尺寸。

只读不改文件（D-5）。任何解码失败都降级为空字段而不是抛出——一张坏文件
不许拖垮整次扫描（file_manager 提取器"失败不阻塞"姿势）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

# EXIF tag ids
_TAG_DATETIME_ORIGINAL = 36867  # 0x9003, Exif IFD
_TAG_SUBSEC_TIME_ORIGINAL = 37520  # 0x9290
_TAG_MAKE = 271
_TAG_MODEL = 272
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_DT_FORMAT = "%Y:%m:%d %H:%M:%S"


@dataclass
class ExifData:
    taken_at: datetime | None = None
    gps_lat: float | None = None
    gps_lng: float | None = None
    make: str | None = None
    model: str | None = None
    width: int | None = None
    height: int | None = None


def parse_datetime(raw: object, subsec: object = None) -> datetime | None:
    """'2025:08:31 14:02:03' (+ '.45' 毫秒) → naive datetime。解析不了返回 None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.strptime(raw.strip(), _DT_FORMAT)
    except ValueError:
        return None
    if isinstance(subsec, str) and subsec.strip():
        # SubSecTimeOriginal 不带前导点，是秒的小数部分
        digits = subsec.strip().lstrip(".")
        if digits.isdigit():
            dt += timedelta(seconds=int(digits[:3]) / 10 ** min(len(digits), 3))
    return dt


def dms_to_degrees(ref: object, d: object, m: object, s: object) -> float | None:
    """度分秒（有理数三元组 + N/S/E/W 方位）→ 十进制度。"""
    try:
        deg = _num(d) + _num(m) / 60 + _num(s) / 3600
    except (TypeError, ValueError):
        return None
    if isinstance(ref, str) and ref.strip().upper() in ("S", "W"):
        deg = -deg
    return deg


def _num(value: object) -> float:
    if isinstance(value, (int, float, Fraction)):
        return float(value)
    # Pillow IFDRational 有 numerator/denominator；也可被 float() 直接转
    return float(value)  # type: ignore[arg-type,call-overload]


def read_exif(path: Path) -> ExifData:
    """读取一张照片的元数据。坏文件返回全空对象（尺寸可能仍可得）。"""
    data = ExifData()
    try:
        with Image.open(path) as img:
            data.width, data.height = img.size
            exif = img.getexif()
    except Exception:  # noqa: BLE001 - 单张坏文件不升级为扫描失败
        return data
    ifd = _get_ifd(exif, _IFD_EXIF)
    data.taken_at = parse_datetime(
        ifd.get(_TAG_DATETIME_ORIGINAL), ifd.get(_TAG_SUBSEC_TIME_ORIGINAL)
    )
    data.make = _text(exif.get(_TAG_MAKE))
    data.model = _text(exif.get(_TAG_MODEL))
    gps = _get_ifd(exif, _IFD_GPS)
    if gps:
        lat = dms_to_degrees(gps.get(1), *_dms(gps.get(2)))
        lng = dms_to_degrees(gps.get(3), *_dms(gps.get(4)))
        if lat is not None and lng is not None:
            data.gps_lat, data.gps_lng = lat, lng
    return data


def _get_ifd(exif: object, tag: int) -> dict:
    """子 IFD 访问统一降级：结构畸形的 EXIF 给空 dict，不抛也不静默吞别的字段。"""
    try:
        result = exif.get_ifd(tag)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return {}
    return dict(result)


def _dms(value: object) -> list[object]:
    items = list(value) if isinstance(value, (list, tuple)) else [None, None, None]
    while len(items) < 3:
        items.append(None)
    return items[:3]


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
