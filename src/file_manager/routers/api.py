"""JSON API：花名册/项目/文件的 CRUD、搜索、上传下载删除。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..identity import resolve_current_user
from ..models import FileMeta, Member, Project, Team
from ..schemas import (
    FileOut,
    MemberCreate,
    MemberOut,
    MemberUpdate,
    ProjectCreate,
    ProjectOut,
    TeamCreate,
    TeamOut,
)
from ..search import SearchQuery, get_backend, list_modes
from ..services.files import (
    DEFAULT_SORT,
    FileFilters,
    delete_file,
    get_file,
    list_files,
    store_upload,
)

router = APIRouter(prefix="/api")


@router.get("/modes")
def search_modes() -> dict:
    return {"modes": list_modes()}


# ---------- 团队与成员（花名册） ----------


@router.get("/teams")
def list_teams(db: Session = Depends(get_db)) -> list[TeamOut]:
    rows = db.execute(
        select(Team.id, Team.name, func.count(Member.id))
        .outerjoin(Member)
        .group_by(Team.id)
        .order_by(Team.name)
    ).all()
    return [TeamOut(id=r[0], name=r[1], member_count=r[2]) for r in rows]


@router.post("/teams", status_code=status.HTTP_201_CREATED)
def create_team(payload: TeamCreate, db: Session = Depends(get_db)) -> TeamOut:
    if db.scalar(select(Team).where(Team.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "同名团队已存在")
    team = Team(name=payload.name)
    db.add(team)
    db.commit()
    db.refresh(team)
    return TeamOut(id=team.id, name=team.name, member_count=0)


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: int, db: Session = Depends(get_db)) -> None:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "团队不存在")
    db.delete(team)
    db.commit()  # 成员的 team_id 由外键 SET NULL；文件保留团队名快照


@router.get("/members")
def list_members(
    team_id: int | None = None, db: Session = Depends(get_db)
) -> list[MemberOut]:
    stmt = (
        select(Member, Team.name)
        .outerjoin(Team, Member.team_id == Team.id)
        .order_by(Member.name)
    )
    if team_id:
        stmt = stmt.where(Member.team_id == team_id)
    rows = db.execute(stmt).all()
    return [
        MemberOut(id=m.id, name=m.name, team_id=m.team_id, team_name=tname)
        for m, tname in rows
    ]


@router.post("/members", status_code=status.HTTP_201_CREATED)
def create_member(payload: MemberCreate, db: Session = Depends(get_db)) -> MemberOut:
    if db.scalar(select(Member).where(Member.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "同名成员已存在")
    _check_team(db, payload.team_id)
    member = Member(name=payload.name, team_id=payload.team_id)
    db.add(member)
    db.commit()
    db.refresh(member)
    return MemberOut(
        id=member.id, name=member.name, team_id=member.team_id, team_name=None
    )


@router.patch("/members/{member_id}")
def update_member(
    member_id: int, payload: MemberUpdate, db: Session = Depends(get_db)
) -> MemberOut:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != member.name:
        if db.scalar(select(Member).where(Member.name == changes["name"])):
            raise HTTPException(status.HTTP_409_CONFLICT, "同名成员已存在")
        member.name = changes["name"]
    if "team_id" in changes:
        _check_team(db, changes["team_id"])
        member.team_id = changes["team_id"]
    db.commit()
    db.refresh(member)
    return MemberOut(
        id=member.id,
        name=member.name,
        team_id=member.team_id,
        team_name=member.team.name if member.team else None,
    )


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(member_id: int, db: Session = Depends(get_db)) -> None:
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    db.delete(member)
    db.commit()  # 文件的 uploader_name 快照保留，可追溯


def _check_team(db: Session, team_id: int | None) -> None:
    if team_id is not None and db.get(Team, team_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "团队不存在")


# ---------- 项目 ----------


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)) -> list[ProjectOut]:
    rows = db.execute(
        select(Project.id, Project.name, Project.description, func.count(FileMeta.id))
        .outerjoin(FileMeta)
        .group_by(Project.id)
        .order_by(Project.name)
    ).all()
    return [
        ProjectOut(id=r[0], name=r[1], description=r[2], file_count=r[3]) for r in rows
    ]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    if db.scalar(select(Project).where(Project.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "同名项目已存在")
    project = Project(name=payload.name, description=payload.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return ProjectOut(id=project.id, name=project.name, description=project.description)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, db: Session = Depends(get_db)) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    if db.scalar(
        select(func.count())
        .select_from(FileMeta)
        .where(FileMeta.project_id == project_id)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "项目下还有文件，不能删除")
    db.delete(project)
    db.commit()


# ---------- 文件 ----------


@router.get("/files")
def files_list(
    project_id: int | None = None,
    team_id: int | None = None,
    uploader_id: int | None = None,
    extension: str | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    db: Session = Depends(get_db),
) -> dict:
    filters = FileFilters(
        project_id=project_id,
        team_id=team_id,
        uploader_id=uploader_id,
        extension=extension,
        sort=sort,
        page=page,
    )
    rows, total = list_files(db, filters)
    return {"total": total, "files": [FileOut.model_validate(r) for r in rows]}


@router.post("/files", status_code=status.HTTP_201_CREATED)
def upload_file(
    request: Request,
    file: UploadFile = File(...),
    project_id: int = Form(...),
    notes: str = Form(""),
    tags: str = Form(""),
    db: Session = Depends(get_db),
) -> FileOut:
    uploader = resolve_current_user(request, db)
    meta = store_upload(
        db, request.app.state.settings, file, project_id, uploader, notes, tags
    )
    return FileOut.model_validate(meta)


@router.get("/files/{file_id}")
def file_detail(file_id: int, db: Session = Depends(get_db)) -> FileOut:
    return FileOut.model_validate(get_file(db, file_id))


@router.get("/files/{file_id}/download")
def download_file(
    request: Request, file_id: int, db: Session = Depends(get_db)
) -> FileResponse:
    import mimetypes

    meta = get_file(db, file_id)
    path = request.app.state.settings.files_dir / meta.rel_path
    media_type = (
        mimetypes.guess_type(meta.original_filename)[0] or "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type, filename=meta.original_filename)


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_file(request: Request, file_id: int, db: Session = Depends(get_db)) -> None:
    delete_file(db, request.app.state.settings, file_id)


# ---------- 搜索 ----------


@router.get("/search")
def search(
    q: str = "",
    mode: str = "metadata",
    project_id: int | None = None,
    team_id: int | None = None,
    uploader_id: int | None = None,
    extension: str | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    db: Session = Depends(get_db),
) -> dict:
    try:
        backend = get_backend(mode)
    except KeyError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"未知搜索模式: {mode}"
        ) from None
    if not backend.available:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "该搜索模式尚未实现")
    result = backend.search(
        db,
        SearchQuery(
            q=q,
            mode=mode,
            project_id=project_id,
            team_id=team_id,
            uploader_id=uploader_id,
            extension=extension,
            sort=sort,
            page=page,
        ),
    )
    return {
        "mode": result.mode,
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "relaxed": result.relaxed,
        "hits": [asdict(h) for h in result.hits],
    }
