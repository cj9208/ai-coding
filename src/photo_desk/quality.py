"""清晰度分：拉普拉斯方差的纯函数层（设计文档 §4.4）。

不碰文件系统、不 import config——和 quantdesk `signals.py` 同姿势：
输入灰度数组 / Pillow 图像，输出标量。判定（组内排名、阈值 0.5×best）
在 triage.py，这里只给分数。

刻意不引 opencv：3×3 四点邻域拉普拉斯核用 numpy 平移切片手搓，一次向量化完成。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

#: 评分前的统一缩略长边。连拍组内张张同尺，分数才可比；也压住 SMB 解码成本。
_EVAL_EDGE = 512


def laplacian_variance(gray: np.ndarray) -> float:
    """拉普拉斯核 (0 1 0 / 1 -4 1 / 0 1 0) 响应的方差。模糊 → 高频消失 → 方差小。"""
    g = gray.astype(np.float64)
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4.0 * g[1:-1, 1:-1]
    return float(lap.var())


def sharpness_of(img: Image.Image) -> float:
    """任意 PIL 图像 → 清晰度分。方向先归正（EXIF orientation 不该影响评分）。"""
    img = ImageOps.exif_transpose(img)
    if img.mode != "L":
        img = img.convert("L")
    img.thumbnail((_EVAL_EDGE, _EVAL_EDGE))
    if min(img.size) < 3:
        return 0.0
    return laplacian_variance(np.asarray(img))


def score_file(path: Path) -> float | None:
    """读文件算分。解码失败返回 None（该照片不参与自动判定，绝不错杀）。"""
    try:
        with Image.open(path) as img:
            return sharpness_of(img)
    except Exception:  # noqa: BLE001 - 坏文件只是没分数，不是结论
        return None
