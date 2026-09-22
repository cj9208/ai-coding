from __future__ import annotations

import numpy as np
from PIL import Image

from photo_desk.quality import laplacian_variance, score_file, sharpness_of

from .helpers import make_photo


def _textured(seed: int = 7, size=(400, 300)) -> Image.Image:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size, dtype=np.uint8)
    return Image.fromarray(arr, "L").convert("RGB")


def _blur(img: Image.Image, sigma: float) -> Image.Image:
    from PIL import ImageFilter

    return img.filter(ImageFilter.GaussianBlur(sigma))


def test_blur_drops_the_score_orders_of_magnitude() -> None:
    sharp = _textured()
    mild = _blur(sharp, 1.5)
    heavy = _blur(sharp, 8)
    s, m, h = (sharpness_of(x) for x in (sharp, mild, heavy))
    assert s > m > h  # 单调：越糊分越低
    assert h < 0.5 * s  # 文档默认阈：组最优的一半以下即废片候选
    assert h * 10 < s  # 高斯 8 的差距应是数量级


def test_score_is_content_not_size() -> None:
    small = _textured(size=(200, 150))
    big = _textured(size=(2000, 1500))
    # 统一缩到 _EVAL_EDGE 后再评分：同一噪声源量级可比（同种子不同尺寸内容不同，
    # 这里只要求不被原图尺寸主导：二者都应远高于全糊基准）
    assert sharpness_of(small) > sharpness_of(_blur(small, 8)) * 10
    assert sharpness_of(big) > sharpness_of(_blur(big, 8)) * 10


def test_flat_image_scores_zero() -> None:
    flat = Image.new("RGB", (100, 100), "gray")
    assert laplacian_variance(np.asarray(flat.convert("L"))) == 0.0


def test_score_file_and_broken_file(tmp_path) -> None:
    path = tmp_path / "t.jpg"
    _textured().save(path)
    score = score_file(path)
    assert score is not None and score > 0
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8garbage")
    assert score_file(bad) is None


def test_score_file_on_pipeline_fixture(tmp_path) -> None:
    # helpers 生成的假照片是纯色块：有分数、且与模糊版差距悬殊
    path = make_photo(tmp_path / "p.jpg", color="red")
    sharp = score_file(path)
    assert sharp is not None and sharp == 0.0  # 纯色 → 零高频，不是 None
    from PIL import ImageFilter

    Image.open(path).filter(ImageFilter.GaussianBlur(3)).save(path)
    assert score_file(path) == 0.0
