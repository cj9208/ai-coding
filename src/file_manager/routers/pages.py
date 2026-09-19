"""HTML 页面：服务端渲染 + 表单提交，数据仍走 services/search 层。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..identity import resolve_current_user
from ..models import FileMeta, Member, Project, Team
from ..search import SearchQuery, get_backend, list_modes
from ..services.files import (
    DEFAULT_SORT,
    SORT_OPTIONS,
    FileFilters,
    delete_file,
    list_files,
    query_string,
    store_upload,
)

router = APIRouter()


def templates(request: Request):
    return request.app.state.templates


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(url: str) -> str:
    return url if url.startswith("/") else "/"


def _opt_int(raw: str | None) -> int | None:
    """下拉框没选时表单提交的是空串（`uploader_id=`），按"无此条件"处理。

    直接声明成 `int | None` 会让 FastAPI 在解析阶段就抛 422，浏览器于是跳到
    一页 JSON 错误信息。非数字同样当作没填。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _page(raw: str | None) -> int:
    return max(_opt_int(raw) or 1, 1)


# ---------- 项目视图 ----------


@router.get("/projects")
def projects_page(
    request: Request, msg: str | None = None, db: Session = Depends(get_db)
):
    rows = db.execute(
        select(Project.id, Project.name, Project.description, func.count(FileMeta.id))
        .outerjoin(FileMeta)
        .group_by(Project.id)
        .order_by(Project.name)
    ).all()
    projects = [
        {"id": r[0], "name": r[1], "description": r[2], "file_count": r[3]}
        for r in rows
    ]
    return templates(request).TemplateResponse(
        request, "projects.html", {"projects": projects, "msg": msg}
    )


@router.post("/projects")
def create_project_form(
    name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)
):
    name = name.strip()
    if not name:
        return redirect("/projects?msg=项目名称不能为空")
    if db.scalar(select(Project).where(Project.name == name)):
        return redirect("/projects?msg=同名项目已存在")
    db.add(Project(name=name, description=description))
    db.commit()
    return redirect("/projects?msg=项目已创建")


@router.get("/projects/{project_id}")
def project_detail(
    request: Request,
    project_id: int,
    team_id: str | None = None,
    uploader_id: str | None = None,
    extension: str | None = None,
    sort: str = DEFAULT_SORT,
    page: str | None = None,
    msg: str | None = None,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    team_id_v = _opt_int(team_id)
    uploader_id_v = _opt_int(uploader_id)
    page_v = _page(page)
    filters = FileFilters(
        project_id=project_id,
        team_id=team_id_v,
        uploader_id=uploader_id_v,
        extension=extension,
        sort=sort,
        page=page_v,
    )
    files, total = list_files(db, filters)
    path = f"/projects/{project_id}"
    base = query_string(
        team_id=team_id_v,
        uploader_id=uploader_id_v,
        extension=extension,
        sort=sort if sort != DEFAULT_SORT else None,
    )
    return templates(request).TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "files": files,
            "total": total,
            "page": page_v,
            "page_size": filters.page_size,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "base": base,
            "next": f"{path}?{base}" if base else path,
            "teams": db.scalars(select(Team).order_by(Team.name)).all(),
            "members": db.scalars(select(Member).order_by(Member.name)).all(),
            "filters": filters,
            "msg": msg,
        },
    )


# ---------- 团队视图 ----------


@router.get("/teams")
def teams_page(request: Request, msg: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Team.id, Team.name, func.count(Member.id))
        .outerjoin(Member)
        .group_by(Team.id)
        .order_by(Team.name)
    ).all()
    teams = [{"id": r[0], "name": r[1], "member_count": r[2]} for r in rows]
    return templates(request).TemplateResponse(
        request, "teams.html", {"teams": teams, "msg": msg}
    )


@router.post("/teams")
def create_team_form(name: str = Form(...), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return redirect("/teams?msg=团队名称不能为空")
    if db.scalar(select(Team).where(Team.name == name)):
        return redirect("/teams?msg=同名团队已存在")
    db.add(Team(name=name))
    db.commit()
    return redirect("/teams?msg=团队已创建")


@router.get("/teams/{team_id}")
def team_detail(
    request: Request,
    team_id: int,
    member_id: str | None = None,
    project_id: str | None = None,
    extension: str | None = None,
    sort: str = DEFAULT_SORT,
    page: str | None = None,
    msg: str | None = None,
    db: Session = Depends(get_db),
):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "团队不存在")
    member_id_v = _opt_int(member_id)
    project_id_v = _opt_int(project_id)
    page_v = _page(page)
    member_counts = db.execute(
        select(Member.id, Member.name, func.count(FileMeta.id))
        .outerjoin(FileMeta, FileMeta.uploader_id == Member.id)
        .where(Member.team_id == team_id)
        .group_by(Member.id)
        .order_by(Member.name)
    ).all()
    members = [{"id": r[0], "name": r[1], "file_count": r[2]} for r in member_counts]
    # 可加入本团队的成员：当前不在本团队的所有人（含未归属任何团队的）
    available_members = db.scalars(
        select(Member)
        .where(or_(Member.team_id.is_(None), Member.team_id != team_id))
        .order_by(Member.name)
    ).all()
    filters = FileFilters(
        project_id=project_id_v,
        team_id=team_id,
        uploader_id=member_id_v,
        extension=extension,
        sort=sort,
        page=page_v,
    )
    files, total = list_files(db, filters)
    path = f"/teams/{team_id}"
    base = query_string(
        member_id=member_id_v,
        project_id=project_id_v,
        extension=extension,
        sort=sort if sort != DEFAULT_SORT else None,
    )
    return templates(request).TemplateResponse(
        request,
        "team_detail.html",
        {
            "team": team,
            "members": members,
            "available_members": available_members,
            "teams": db.scalars(select(Team).order_by(Team.name)).all(),
            "files": files,
            "total": total,
            "page": page_v,
            "page_size": filters.page_size,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "base": base,
            "next": f"{path}?{base}" if base else path,
            "projects": db.scalars(select(Project).order_by(Project.name)).all(),
            "filters": filters,
            "msg": msg,
        },
    )


@router.post("/members")
def create_member_form(
    name: str = Form(...),
    team_id: str | None = Form(None),
    next: str = Form("/teams"),
    db: Session = Depends(get_db),
):
    """成员唯一创建入口。team_id 下拉的"（不归属）"提交的是空串。"""
    name = name.strip()
    if name and not db.scalar(select(Member).where(Member.name == name)):
        db.add(Member(name=name, team_id=_opt_int(team_id)))
        db.commit()
        return redirect(f"{_safe_next(next)}?msg=成员已添加")
    return redirect(f"{_safe_next(next)}?msg=成员未添加（重名或为空）")


@router.post("/members/{member_id}/assign")
def assign_member_form(
    member_id: int,
    team_id: str | None = Form(None),
    next: str = Form("/teams"),
    db: Session = Depends(get_db),
):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(404, "成员不存在")
    member.team_id = _opt_int(team_id)
    db.commit()
    return redirect(f"{_safe_next(next)}?msg=已调整 {member.name} 的团队归属")


@router.post("/teams/{team_id}/add-member")
def team_add_member_form(
    team_id: int,
    member_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """把已有成员加入团队：只调整归属，不创建成员（成员的唯一创建入口是 POST /members）。"""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "团队不存在")
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(404, "成员不存在")
    member.team_id = team_id
    db.commit()
    return redirect(f"/teams/{team_id}?msg=已把 {member.name} 加入 {team.name}")


# ---------- 文件页：浏览 + 检索同一个视图 ----------


@router.get("/")
@router.get("/search")
def browse_page(
    request: Request,
    q: str = "",
    mode: str = "metadata",
    project_id: str | None = None,
    team_id: str | None = None,
    uploader_id: str | None = None,
    extension: str | None = None,
    sort: str = DEFAULT_SORT,
    page: str | None = None,
    msg: str | None = None,
    db: Session = Depends(get_db),
):
    """浏览与检索合并：无关键词时列出全部文件，填了条件就当场缩窄。

    关键词走搜索后端（附带命中来源标签），纯筛选走 list_files，
    两者都用同一套排序参数与同一个 file_table 渲染。
    """
    project_id_v = _opt_int(project_id)
    team_id_v = _opt_int(team_id)
    uploader_id_v = _opt_int(uploader_id)
    page_v = _page(page)

    error = None
    result = None
    match_map: dict[int, list[str]] | None = None

    if q:
        try:
            backend = get_backend(mode)
        except KeyError:
            error = f"未知搜索模式：{mode}"
        else:
            if not backend.available:
                error = f"「{backend.description}」尚未实现，敬请期待"
            else:
                result = backend.search(
                    db,
                    SearchQuery(
                        q=q,
                        mode=mode,
                        project_id=project_id_v,
                        team_id=team_id_v,
                        uploader_id=uploader_id_v,
                        extension=extension,
                        sort=sort,
                        page=page_v,
                    ),
                )
                match_map = {h.id: h.matched for h in result.hits}
                files = _files_in_hit_order(db, result.hits)
                total, page_size = result.total, result.page_size

    if result is None:  # 无关键词，或模式不可用
        filters = FileFilters(
            project_id=project_id_v,
            team_id=team_id_v,
            uploader_id=uploader_id_v,
            extension=extension,
            sort=sort,
            page=page_v,
        )
        files, total = list_files(db, filters)
        page_size = filters.page_size

    # 分页/删除回跳要保留当前条件，所以把条件串单独拼出来（不含 page）
    base = query_string(
        q=q,
        mode=mode if q else None,
        project_id=project_id_v,
        team_id=team_id_v,
        uploader_id=uploader_id_v,
        extension=extension,
        sort=sort if sort != DEFAULT_SORT else None,
    )
    return templates(request).TemplateResponse(
        request,
        "browse.html",
        {
            "q": q,
            "mode": mode,
            "modes": list_modes(),
            "result": result,
            "files": files,
            "total": total,
            "match_map": match_map,
            "error": error,
            "page": page_v,
            "page_size": page_size,
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "base": base,
            "next": f"{request.url.path}?{base}" if base else request.url.path,
            "teams": db.scalars(select(Team).order_by(Team.name)).all(),
            "members": db.scalars(select(Member).order_by(Member.name)).all(),
            "projects": db.scalars(select(Project).order_by(Project.name)).all(),
            "filters": {
                "project_id": project_id_v,
                "team_id": team_id_v,
                "uploader_id": uploader_id_v,
                "extension": extension,
            },
            "msg": msg,
        },
    )


def _files_in_hit_order(db: Session, hits) -> list[FileMeta]:
    """按命中顺序换回完整的 FileMeta 行（页面需要标签、备注、删除按钮）。"""
    if not hits:
        return []
    order = {h.id: i for i, h in enumerate(hits)}
    rows = db.scalars(
        select(FileMeta)
        .where(FileMeta.id.in_(order))
        .options(selectinload(FileMeta.project))
    ).all()
    return sorted(rows, key=lambda f: order[f.id])


# ---------- 上传与删除（HTML 表单入口） ----------


@router.post("/files")
def upload_form(
    request: Request,
    file: UploadFile = File(...),
    project_id: int = Form(...),
    notes: str = Form(""),
    tags: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    uploader = resolve_current_user(request, db)
    store_upload(
        db, request.app.state.settings, file, project_id, uploader, notes, tags
    )
    target = _safe_next(next) if next else f"/projects/{project_id}"
    return redirect(f"{target}{'&' if '?' in target else '?'}msg=上传成功")


@router.post("/files/{file_id}/delete")
def delete_form(
    request: Request,
    file_id: int,
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    delete_file(db, request.app.state.settings, file_id)
    return redirect(
        _safe_next(next) + ("&" if "?" in _safe_next(next) else "?") + "msg=已删除"
    )
