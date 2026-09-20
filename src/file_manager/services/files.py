"""文件服务：上传管道（校验→落盘→哈希→提取→入库→FTS）与列表查询。"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..extractors import extract_for
from ..fts import FILES_INDEX, fts_values
from ..models import FileMeta, Member, Project

_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_stem(name: str) -> str:
    """保留中文的文件名清洗：只替换路径危险字符，不像 werkzeug 那样剥离非 ASCII。"""
    stem = Path(name).stem or "file"
    stem = _FORBIDDEN_CHARS.sub("_", stem).strip(" .")
    return (stem or "file")[:80]


def query_string(**params) -> str:
    """拼出保留当前筛选/排序条件的查询串（不含 page），供分页链接使用。"""
    kept = {k: v for k, v in params.items() if v not in (None, "", 0)}
    return urlencode(kept)


#: 排序选项：值为 URL 里的 sort 参数，元组第二项供前端下拉框直接显示。
#: 中文按码位排序（SQLite BINARY 排序规则），不是拼音序——够用且稳定。
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("created_at_desc", "创建时间（新→旧）"),
    ("created_at_asc", "创建时间（旧→新）"),
    ("filename_asc", "文件名（A→Z）"),
    ("filename_desc", "文件名（Z→A）"),
    ("size_desc", "大小（大→小）"),
    ("size_asc", "大小（小→大）"),
    ("title_asc", "标题（A→Z）"),
    ("title_desc", "标题（Z→A）"),
)

DEFAULT_SORT = SORT_OPTIONS[0][0]

_SORT_COLUMNS = {
    "created_at": FileMeta.created_at,
    "filename": FileMeta.original_filename,
    "size": FileMeta.size,
    "title": FileMeta.title,
}


@dataclass
class FileFilters:
    project_id: int | None = None
    team_id: int | None = None
    uploader_id: int | None = None
    extension: str | None = None
    sort: str = DEFAULT_SORT
    page: int = 1
    page_size: int = 50


def order_by_for(sort: str) -> list:
    """把 sort 参数（形如 field_asc / field_desc）换成 ORDER BY 子句，未知值回退到新→旧。"""
    field, _, direction = sort.rpartition("_")
    column = _SORT_COLUMNS.get(field)
    if column is None:
        primary = FileMeta.created_at.desc()
    else:
        primary = column.desc() if direction == "desc" else column.asc()
    # 再按 id 兜底：同一时间戳/同名/同大小也要稳定顺序，否则翻页会跳行
    return [primary, FileMeta.id.desc()]


def _conds(filters: FileFilters):
    conds = []
    if filters.project_id:
        conds.append(FileMeta.project_id == filters.project_id)
    if filters.team_id:
        conds.append(FileMeta.team_id == filters.team_id)
    if filters.uploader_id:
        conds.append(FileMeta.uploader_id == filters.uploader_id)
    if filters.extension:
        conds.append(FileMeta.extension == filters.extension.lstrip(".").lower())
    return conds


def list_files(db: Session, filters: FileFilters) -> tuple[list[FileMeta], int]:
    conds = _conds(filters)
    total = db.scalar(select(func.count()).select_from(FileMeta).where(*conds)) or 0
    page = max(filters.page, 1)
    stmt = (
        select(FileMeta)
        .where(*conds)
        .options(selectinload(FileMeta.project))
        .order_by(*order_by_for(filters.sort))
        .offset((page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return list(db.scalars(stmt).all()), total


def store_upload(
    db: Session,
    settings: Settings,
    upload: UploadFile,
    project_id: int,
    uploader: Member,
    notes: str = "",
    tags: str = "",
) -> FileMeta:
    """上传管道。同项目内相同内容重复上传是幂等的：直接返回已有记录。"""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")

    data = upload.file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件内容为空")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"文件超过大小限制（{settings.max_upload_mb}MB）",
        )

    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(
        select(FileMeta).where(
            FileMeta.project_id == project_id, FileMeta.content_hash == digest
        )
    )
    if existing is not None:
        return existing

    original = upload.filename or "unnamed"
    ext = Path(original).suffix.lower()
    rel_path = f"{project_id}/{uuid.uuid4().hex[:12]}_{sanitize_stem(original)}{ext}"
    dest = settings.files_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    extracted = extract_for(dest)
    meta = FileMeta(
        original_filename=original,
        rel_path=rel_path,
        content_hash=digest,
        size=len(data),
        extension=ext.lstrip(".") or None,
        project_id=project.id,
        uploader_id=uploader.id,
        uploader_name=uploader.name,
        team_id=uploader.team_id,
        team_name=uploader.team.name if uploader.team is not None else None,
        title=extracted.title,
        notes=notes or "",
        tags=tags or "",
        extracted_text=(
            extracted.text[: settings.extracted_text_cap] if extracted.text else None
        ),
    )
    db.add(meta)
    db.flush()
    FILES_INDEX.upsert(db, meta.id, fts_values(meta))
    db.commit()
    db.refresh(meta)
    return meta


def delete_file(db: Session, settings: Settings, file_id: int) -> None:
    meta = db.get(FileMeta, file_id)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    FILES_INDEX.delete(db, meta.id)
    siblings = (
        db.scalar(
            select(func.count())
            .select_from(FileMeta)
            .where(FileMeta.rel_path == meta.rel_path)
        )
        or 0
    )
    db.delete(meta)
    db.commit()
    if siblings <= 1:  # 磁盘上没有被其他记录引用才真正删除
        (settings.files_dir / meta.rel_path).unlink(missing_ok=True)


def get_file(db: Session, file_id: int) -> FileMeta:
    meta = db.get(FileMeta, file_id)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    return meta


def rebuild_fts(db: Session) -> int:
    """清空并按当前折叠规则重建全文索引，返回重建的记录数。"""
    db.execute(text("DELETE FROM files_fts"))
    metas = db.scalars(select(FileMeta)).all()
    for meta in metas:
        FILES_INDEX.upsert(db, meta.id, fts_values(meta))
    db.commit()
    return len(metas)
