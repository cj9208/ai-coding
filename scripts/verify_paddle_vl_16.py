"""PaddleOCR-VL 1.6 真机验证脚本 —— 对应 docs/ocr-backend-design.md §5.3 六项清单。

  1. PDF 页面栅格宽高来源（res 里是否有尺寸字段；没有则推断内部栅格 DPI）
  2. block_order 实际形态（连续整数？哪些为 null？与 block_id 的关系）
  3. 表格 / 公式块 content 的包裹形态（HTML 表？LaTeX？是否带 $）/ format_block_content 的影响
  4. use_layout_detection=False 时的输出形态
  5. 空白页 / 纯图页的 res 结构（缺 key 还是空列表）
  6. 单页推理耗时与显存基线（本机 GPU）

用法（仓库根目录，环境需先 `uv sync --extra ocr --extra paddle-gpu`）：
    .venv/Scripts/python.exe shell_scipts/verify_paddle_vl_16.py

产物全部写入 data/ocr_backend/verify/（gitignored）：
    materials/   自造测试 PDF（6 页：页眉页脚+标题正文 / 表格 / 公式 / 空白 / 柱图 / 单栏长文）与 page1.png
    runA_pdf_default/   PDF 输入，默认参数（format_block_content=False）
    runB_pdf_markdown/  PDF 输入，format_block_content=True
    runC_img_nolayout/  page1.png 单图，use_layout_detection=False
stdout 为结构化报告。
"""

from __future__ import annotations

import gc
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "ocr_backend" / "verify"
MAT = OUT / "materials"

A4_W, A4_H = 595.3, 841.9
PAGE1_DPI = 150

CJK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]
MATH_FONTS = [r"C:\Windows\Fonts\cambria.ttc"]

PNG_DIMS: dict[str, tuple[int, int]] = {}


# ---------------------------------------------------------------- materials
def _fontfile_or_none(cands: list[str]) -> str | None:
    for c in cands:
        if Path(c).exists():
            return c
    return None


def build_pdf(path: Path) -> None:
    import fitz

    cjk = _fontfile_or_none(CJK_FONTS)
    math = _fontfile_or_none(MATH_FONTS)
    print(f"[materials] cjk font: {cjk}, math font: {math}")

    def putbox(page, rect, text, size=10.5, fontfile=None, name="helv", align=0):
        kw = {"fontsize": size, "fontname": name, "align": align}
        if fontfile:
            kw["fontfile"] = fontfile
        r = page.insert_textbox(fitz.Rect(rect), text, **kw)
        if r < 0:
            print(f"  ! textbox overflow ({r}) for: {text[:30]!r}")
        return r

    doc = fitz.open()

    # -- page 0: header + title + paragraphs (zh/en)
    p = doc.new_page(width=A4_W, height=A4_H)
    p.insert_text(
        (50, 36),
        "AI Coding 内部技术报告 · 第 3 期",
        fontsize=8.5,
        fontfile=cjk or "",
        fontname="hdr" if cjk else "helv",
    )
    putbox(
        p,
        (50, 60, 545, 105),
        "PaddleOCR-VL 1.6 Output Verification",
        size=20,
        name="hebo",
    )
    putbox(
        p,
        (50, 120, 545, 250),
        "This page is test material for verifying the native output structure of "
        "PaddleOCR-VL 1.6. It mixes plain English paragraphs with a Chinese "
        "paragraph so the adapter mapping can be checked for both scripts. "
        "The layout detector should classify the heading above as a document "
        "title and everything here as body text.",
        size=11,
    )
    putbox(
        p,
        (50, 265, 545, 340),
        "本页用于验证 PaddleOCR-VL 1.6 的原生输出结构：段落文本是否按块返回、"
        "阅读顺序是否连续、页眉页脚是否被单独识别。中文段落同时用于检查识别结果"
        "中的 CJK 文本是否原样保留。",
        size=11,
        fontfile=cjk,
    )
    p.insert_text(
        (50, 812), "printed at 2026-09-21 · internal use only · page 1", fontsize=8.5
    )

    # -- page 1: table with caption
    p = doc.new_page(width=A4_W, height=A4_H)
    putbox(p, (60, 100, 535, 130), "表 1：验证用汇总表", size=12, fontfile=cjk, align=1)
    x0, y0, rows, cols, cw, rh = 70, 150, 4, 3, 150, 34
    for r in range(rows + 1):
        p.draw_line((x0, y0 + r * rh), (x0 + cols * cw, y0 + r * rh), width=0.8)
    for c in range(cols + 1):
        p.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + rows * rh), width=0.8)
    cells = [
        ("Model", "Params", "OmniDocBench v1.6"),
        ("PaddleOCR-VL-1.5", "0.9B", "94.2"),
        ("PaddleOCR-VL-1.6", "0.9B", "96.33"),
        ("MinerU", "1.2B", "90.4"),
    ]
    for r, row in enumerate(cells):
        for c, cell in enumerate(row):
            fname = "hebo" if r == 0 else "helv"
            p.insert_text(
                (x0 + c * cw + 8, y0 + r * rh + 22), cell, fontsize=10.5, fontname=fname
            )
    putbox(
        p, (70, 320, 520, 350), "数据为验证素材，非官方数字。", size=9.5, fontfile=cjk
    )
    p.insert_text((50, 812), "page 2", fontsize=8.5)

    # -- page 2: display formula + inline formula
    p = doc.new_page(width=A4_W, height=A4_H)
    p.insert_text(
        (60, 90),
        "L = 1/2 \u03c1 v\u00b2 S C_L",
        fontsize=16,
        fontfile=math or "",
        fontname="mth" if math else "helv",
    )
    p.insert_text(
        (520, 90),
        "(1)",
        fontsize=16,
        fontfile=math or "",
        fontname="mth" if math else "helv",
    )
    putbox(
        p,
        (60, 140, 535, 220),
        "The lift equation above relates the lift force to air density, velocity and "
        "wing area. An inline formula a\u00b2 + b\u00b2 = c\u00b2 appears in this paragraph "
        "to check whether inline formulas are treated differently from display formulas.",
        size=11,
    )
    p.insert_text((50, 812), "page 3", fontsize=8.5)

    # -- page 3: intentionally blank
    doc.new_page(width=A4_W, height=A4_H)

    # -- page 4: figure (bar chart) + caption
    p = doc.new_page(width=A4_W, height=A4_H)
    ox, oy, w, h = 110, 180, 380, 260
    p.draw_line((ox, oy), (ox, oy + h), width=1.2)
    p.draw_line((ox, oy + h), (ox + w, oy + h), width=1.2)
    bars = [("1.5", 0.62), ("1.6", 0.86), ("other", 0.45)]
    bw = w / 6
    for i, (tag, frac) in enumerate(bars):
        bx = ox + bw * (i * 2 + 1)
        p.draw_rect(
            fitz.Rect(bx, oy + h - frac * h, bx + bw, oy + h),
            color=(0.2, 0.3, 0.7),
            fill=(0.55, 0.65, 0.9),
        )
        p.insert_text((bx + bw / 4, oy + h + 14), tag, fontsize=9)
    putbox(p, (60, 120, 535, 150), "Accuracy by version (synthetic)", size=12, align=1)
    putbox(
        p,
        (60, 470, 535, 500),
        "图 1：验证用柱状图（合成数据）。",
        size=10,
        fontfile=cjk,
        align=1,
    )
    p.insert_text((50, 812), "page 5", fontsize=8.5)

    # -- page 5: single-column long text
    p = doc.new_page(width=A4_W, height=A4_H)
    putbox(p, (50, 70, 545, 130), "3 Results", size=14, name="hebo")
    putbox(
        p,
        (50, 150, 545, 300),
        "We evaluate the pipeline on a small internal set of documents. "
        "The recognition quality is stable across pages and the reading order "
        "matches the visual order in most cases. This paragraph exists to give "
        "the layout model a plain single-column page without any special blocks.",
        size=11,
    )
    p.insert_text((50, 812), "page 6", fontsize=8.5)

    doc.save(path)
    pix = doc[0].get_pixmap(dpi=PAGE1_DPI)
    png = MAT / "page1.png"
    pix.save(png)
    PNG_DIMS[png.name] = (pix.width, pix.height)
    print(f"[materials] {path.name}: {doc.page_count} pages")
    print(f"[materials] {png.name}: {pix.width}x{pix.height} (page0 @ {PAGE1_DPI} dpi)")
    doc.close()


# ---------------------------------------------------------------- runs
def make_pipeline(**kwargs):
    from paddleocr import PaddleOCRVL

    t0 = time.perf_counter()
    pipe = PaddleOCRVL(pipeline_version="v1.6", **kwargs)
    return pipe, time.perf_counter() - t0


def run_case(
    name: str, source: Path, init_kwargs: dict, live_checks: bool = True
) -> Path:
    import paddle

    run_dir = OUT / name
    for sub in ("json", "md", "img"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    print(f"\n=== run {name}: source={source.name} kwargs={init_kwargs} ===")
    pipe, load_s = make_pipeline(**init_kwargs)
    print(f"  [init] {load_s:.1f}s")
    t_start = t_prev = time.perf_counter()
    n = 0
    for res in pipe.predict(str(source)):
        now = time.perf_counter()
        print(f"  [page {n}] predict {now - t_prev:.1f}s")
        t_prev = now
        if live_checks and n == 0:
            try:
                data = res.json
                raw = json.loads(data) if isinstance(data, str) else data
                blocks = raw["res"].get("parsing_res_list") or []
                if blocks:
                    b = blocks[0]
                    print(
                        f"  [live] first block bbox types: "
                        f"{[type(v).__name__ for v in b['block_bbox']]}"
                    )
                    print(f"  [live] keys of first block: {sorted(b.keys())}")
                print(f"  [live] res top-level keys: {sorted(raw['res'].keys())}")
            except Exception as e:  # diagnostics only
                print(f"  ! live check failed: {e!r}")
        for kind, fn in (
            ("json", res.save_to_json),
            ("md", res.save_to_markdown),
            ("img", res.save_to_img),
        ):
            try:
                fn(save_path=str(run_dir / kind))
            except Exception as e:
                print(f"    ! save_to_{kind} failed: {e!r}")
        n += 1
    total = time.perf_counter() - t_start
    print(f"  [total] {n} pages in {total:.1f}s ({total / max(n, 1):.1f}s/page)")
    if paddle.device.is_compiled_with_cuda():
        print(
            f"  [vram] allocated={paddle.device.cuda.memory_allocated() / 2**30:.2f} GiB "
            f"peak={paddle.device.cuda.max_memory_allocated() / 2**30:.2f} GiB"
        )
    del pipe
    gc.collect()
    if paddle.device.is_compiled_with_cuda():
        paddle.device.cuda.empty_cache()
    return run_dir


# ---------------------------------------------------------------- analysis
def walk(obj, path="$"):
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:12]):
            yield from walk(v, f"{path}[{i}]")


def find_dims(obj) -> list[tuple[str, object]]:
    hits: list[tuple[str, object]] = []
    for path, v in walk(obj):
        key = path.rsplit(".", 1)[-1].lower().rstrip("]0123456789[")
        if key in (
            "width",
            "height",
            "shape",
            "size",
            "image_shape",
            "page_size",
            "img_shape",
        ):
            if isinstance(v, (int, float, str, list, tuple)):
                hits.append((path, v))
    return hits


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = (float(x) for x in a)
    bx1, by1, bx2, by2 = (float(x) for x in b)
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua else 0.0


def load_pages(run_dir: Path) -> list[dict]:
    pages = []
    for f in sorted((run_dir / "json").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        res = data.get("res", data)
        pages.append({"file": f.name, "res": res})
    pages.sort(key=lambda p: p["res"].get("page_index") or 0)
    return pages


def report_page(pg: dict, table: bool = False) -> None:
    res = pg["res"]
    print(f"\n--- {pg['file']} (page_index={res.get('page_index')}) ---")
    print(f"  res keys: {sorted(res.keys())}")
    print(f"  model_settings: {res.get('model_settings')}")
    dims = find_dims(res)
    print(f"  dimension-ish fields: {dims if dims else 'NONE'}")
    lay = res.get("layout_det_res") or {}
    boxes = lay.get("boxes") or []
    labels = [b.get("label") for b in boxes]
    print(f"  layout boxes: {len(boxes)} -> {labels}")
    if boxes:
        co = boxes[0]["coordinate"]
        print(f"  box[0] coordinate types: {[type(v).__name__ for v in co]} value={co}")
        print(f"  box[0] score type: {type(boxes[0]['score']).__name__}")
    blocks = res.get("parsing_res_list") or []
    print(
        f"  blocks: {len(blocks)}  (id/order/label/score via layout-iou/content-head)"
    )
    for b in blocks:
        iou_hit, hit_label = 0.0, None
        for lb in boxes:
            v = iou(b["block_bbox"], lb["coordinate"])
            if v > iou_hit:
                iou_hit, hit_label = v, lb["label"]
        content = (b.get("block_content") or "").replace("\n", "\\n")
        bbox_types = [type(v).__name__ for v in b["block_bbox"]]
        print(
            f"    id={b.get('block_id')} order={b.get('block_order')!r} "
            f"label={b.get('block_label'):<16} bbox_types={bbox_types} "
            f"iou->{hit_label}({iou_hit:.2f}) content[:70]={content[:70]!r}"
        )
        if table and b.get("block_label") in ("table", "formula", "inline_formula"):
            print(f"      FULL content: {content[:600]!r}")


def main() -> int:
    import paddle
    import paddleocr

    OUT.mkdir(parents=True, exist_ok=True)
    MAT.mkdir(parents=True, exist_ok=True)
    print(
        f"# paddleocr {paddleocr.__version__} / paddle {paddle.__version__} / "
        f"device={paddle.device.get_device()}"
    )
    print(f"# output root: {OUT}")

    pdf = MAT / "verify_doc.pdf"
    if not pdf.exists():
        build_pdf(pdf)
    else:
        print(f"[materials] reuse {pdf}")

    # 优先使用仓库内已下载的模型快照，避免重复下载 ~1.9GB 到 ~/.paddlex
    from ocr_backend.models import model_dir

    local_model = model_dir("paddleocr-vl-1.6")
    model_kwargs = {}
    if (local_model / "model.safetensors").exists():
        model_kwargs = {"vl_rec_model_dir": str(local_model)}
        print(f"[model] using local snapshot: {local_model}")
    else:
        print("[model] no local snapshot, will use/download official model")

    runs: list[tuple[str, Path, dict[str, Any]]] = [
        ("runA_pdf_default", pdf, {}),
        ("runB_pdf_markdown", pdf, {"format_block_content": True}),
        ("runC_img_nolayout", MAT / "page1.png", {"use_layout_detection": False}),
    ]
    runs = [(name, src, {**model_kwargs, **kw}) for name, src, kw in runs]
    for name, src, kw in runs:
        try:
            run_case(name, src, kw)
        except Exception:
            print(f"!!! run {name} FAILED")
            traceback.print_exc()

    print("\n\n================ VERIFICATION REPORT ================")
    print(
        f"# reference dims: A4 page = {A4_W}x{A4_H} pts; "
        f"page1.png = {PNG_DIMS.get('page1.png')} px (@{PAGE1_DPI}dpi)"
    )

    for run, title in (
        ("runA_pdf_default", "RUN A (defaults, PDF)"),
        ("runB_pdf_markdown", "RUN B (format_block_content=True, PDF)"),
    ):
        print(f"\n########## {title} ##########")
        try:
            pages = load_pages(OUT / run)
        except Exception as e:
            print(f"  load failed: {e!r}")
            continue
        for pg in pages:
            report_page(pg, table=(run == "runB_pdf_markdown"))
        # saved img sizes -> internal raster guess
        imgs = sorted((OUT / run / "img").glob("*.png")) + sorted(
            (OUT / run / "img").glob("*.jpg")
        )
        for im in imgs[:8]:
            try:
                import fitz

                pm = fitz.Pixmap(str(im))
                print(f"  [img] {im.name}: {pm.width}x{pm.height}")
            except Exception as e:
                print(f"  [img] {im.name}: unreadable ({e!r})")
        mds = sorted((OUT / run / "md").glob("*.md"))
        if mds:
            print(
                f"  [md] {mds[0].name} head:\n{mds[0].read_text(encoding='utf-8')[:400]}"
            )

    print("\n########## RUN C (single image, use_layout_detection=False) ##########")
    try:
        for pg in load_pages(OUT / "runC_img_nolayout"):
            report_page(pg)
    except Exception as e:
        print(f"  load failed: {e!r}")

    print("\n================ END REPORT ================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
