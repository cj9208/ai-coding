"""身份桩：诚实假设下的人工身份。

前端把用户手输的姓名放在 fm_user cookie（或 X-User 头）里，服务端据此解析成员。
首次出现的名字自动注册为成员（暂不归属团队），后续在花名册页面可分配团队。

接入真实身份系统时，只需替换 resolve_current_user 的实现，
所有路由通过这一个入口取当前用户，业务代码不用动。
"""

from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Member

COOKIE_NAME = "fm_user"


def _decode(raw: str | None) -> str | None:
    """身份来源（cookie / X-User 头）的值都是 percent-encoded 的中文名，统一解码。"""
    return unquote(raw) if raw else None


def resolve_current_user(request: Request, db: Session) -> Member:
    name = _decode(request.headers.get("X-User")) or _decode(
        request.cookies.get(COOKIE_NAME)
    )
    if not name or not name.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="未提供身份，请先设置姓名（页面右上角）",
        )
    name = name.strip()
    member = db.scalar(select(Member).where(Member.name == name))
    if member is None:
        member = Member(name=name, team_id=None)
        db.add(member)
        db.commit()
        db.refresh(member)
    return member


def remember_user(response: Response, name: str) -> None:
    response.set_cookie(COOKIE_NAME, quote(name), max_age=365 * 86400, samesite="lax")
