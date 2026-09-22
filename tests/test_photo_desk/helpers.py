"""photo_desk 测试共享件：生成带 EXIF 的假照片目录树。

HEIC 写固件不可用（本机 pillow-heif 轮子只解码不编码，2026-09-22 实测），
故以 JPEG/PNG 覆盖全部管线逻辑；HEIC 真读由真机演练补验（设计文档 §6-3）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, TiffImagePlugin

from photo_desk.config import Settings, load_settings


def make_photo(
    path: Path,
    *,
    color: str = "red",
    size: tuple[int, int] = (200, 100),
    taken_at: datetime | None = None,
    subsec: str | None = None,
    make: str = "TESTMAKE",
    model: str = "TESTMODEL",
    gps: tuple[float, float] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    exif = img.getexif()
    exif[271] = make
    exif[272] = model
    if taken_at is not None:
        ifd = exif.get_ifd(0x8769)
        ifd[36867] = taken_at.strftime("%Y:%m:%d %H:%M:%S")
        if subsec is not None:
            ifd[37520] = subsec
    if gps is not None:
        g = exif.get_ifd(0x8825)
        lat, lng = gps
        g[1] = "N" if lat >= 0 else "S"
        g[2] = _dms(abs(lat))
        g[3] = "E" if lng >= 0 else "W"
        g[4] = _dms(abs(lng))
    if path.suffix.lower() == ".png":
        img.save(path)  # PNG 不带这套 EXIF，用作"无 EXIF 回退文件时间"样本
    else:
        img.save(path, exif=exif)
    return path


def _dms(deg: float):
    d = int(deg)
    m = int((deg - d) * 60)
    s = (deg - d - m / 60) * 3600
    return (
        TiffImagePlugin.IFDRational(round(d), 1),
        TiffImagePlugin.IFDRational(round(m), 1),
        TiffImagePlugin.IFDRational(round(s * 1000), 1000),
    )


def make_settings(tmp_path: Path) -> Settings:
    root = tmp_path / "photo_root"
    root.mkdir(parents=True, exist_ok=True)
    return load_settings(root, tmp_path / "out")
