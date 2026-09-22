"""查看层路由：时间线 + 照片详情 + 派生图。始终只读——隔离/放回写在 CLI
（triage.py），放回按钮归 M2 编辑模式（记录在案的偏离）。quarantined 灰显。"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import PhotoRootError
from ..library import STATE_IN_PLACE, STATE_QUARANTINED, Photo, iter_photos
from ..thumbs import ensure_image

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_db(request: Request):
    with request.app.state.storage.session() as db:
        yield db


@router.get("/")
def timeline(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    try:
        settings.ensure_root()
    except PhotoRootError as exc:
        return templates.TemplateResponse(
            request, "timeline.html", {"root_error": str(exc), "days": []}
        )

    days: "OrderedDict[object, list[Photo]]" = OrderedDict()
    for photo in iter_photos(db, state=None):
        # missing/orphaned 不展示：文件都不在，缩略图无从谈起
        if photo.state not in (STATE_IN_PLACE, STATE_QUARANTINED):
            continue
        days.setdefault(photo.day, []).append(photo)
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {"days": list(days.items()), "root_error": None},
    )


def _get_photo(db: Session, photo_id: int) -> Photo:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "照片不存在")
    return photo


@router.get("/photo/{photo_id}")
def photo_detail(request: Request, photo_id: int, db: Session = Depends(get_db)):
    photo = _get_photo(db, photo_id)
    return templates.TemplateResponse(request, "photo.html", {"photo": photo})


@router.get("/photo/{photo_id}/thumb.webp")
def photo_thumb(request: Request, photo_id: int, db: Session = Depends(get_db)):
    return _derived(request, db, photo_id, "thumb")


@router.get("/photo/{photo_id}/preview.webp")
def photo_preview(request: Request, photo_id: int, db: Session = Depends(get_db)):
    return _derived(request, db, photo_id, "preview")


def _derived(request: Request, db: Session, photo_id: int, kind: str) -> FileResponse:
    photo = _get_photo(db, photo_id)
    path = ensure_image(request.app.state.settings, photo, kind)
    if path is None:
        raise HTTPException(404, "无法派生图像（原件缺失或解码失败）")
    return FileResponse(path, media_type="image/webp")
