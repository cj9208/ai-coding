"""ORM Base + FastAPI 会话依赖。

引擎与 PRAGMA 策略（WAL、外键、监听器挂实例）属于通用层，见
``storage.sqlite``；本模块只保留两样业务侧的东西：项目自己的
``DeclarativeBase``，以及把请求映射到共享客户端会话的 ``get_db``。
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def get_db(request: Request):
    """FastAPI 依赖：每个请求一个会话，结束时关闭。

    ``app.state.storage`` 是 ``storage.SqliteClient``（create_app 里建立）。
    """
    storage = request.app.state.storage
    with storage.session() as db:
        yield db
