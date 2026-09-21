"""文件管理器端到端测试的共享 fixture 与帮助函数。"""

from __future__ import annotations

import io
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from file_manager.app import create_app
from file_manager.config import Settings

EML_BYTES = """From: alice@example.com
To: bob@example.com
Subject: 三季度财报初稿
Date: Mon, 15 Sep 2026 10:00:00 +0800
Content-Type: text/plain; charset="utf-8"

各位好，附件是三季度财报初稿，请查阅。
""".encode("utf-8")


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "fm", db_url=f"sqlite:///{tmp_path / 'fm' / 'fm.db'}"
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def set_identity(client: TestClient, name: str) -> None:
    # 与前端 app.js 一致：cookie 里存 URL 编码后的姓名
    client.cookies.set("fm_user", quote(name))


def make_project(client: TestClient, name: str = "市场雷达") -> int:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_team(client: TestClient, name: str = "数据组") -> int:
    r = client.post("/api/teams", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(
    client: TestClient,
    project_id: int,
    filename: str,
    content: bytes,
    **fields,
) -> dict:
    r = client.post(
        "/api/files",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"project_id": str(project_id), **fields},
    )
    assert r.status_code == 201, r.text
    return r.json()
