"""上传管道：身份、幂等、团队快照、EML 标题提取、删除。"""

from __future__ import annotations

import io

from .conftest import (
    EML_BYTES,
    make_project,
    make_team,
    set_identity,
    upload,
)


def test_upload_and_project_view(client):
    set_identity(client, "张三")
    pid = make_project(client)
    f = upload(
        client,
        pid,
        "会议纪要.txt",
        "本周进展同步".encode("utf-8"),
        tags="周会",
        notes="九月第三周",
    )

    assert f["uploader_name"] == "张三"
    assert f["project_name"] == "市场雷达"

    r = client.get("/api/files", params={"project_id": pid})
    assert r.json()["total"] == 1

    page = client.get(f"/projects/{pid}")
    assert page.status_code == 200
    assert "会议纪要.txt" in page.text


def test_upload_requires_identity(client):
    pid = make_project(client)
    r = client.post(
        "/api/files",
        files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        data={"project_id": pid},
    )
    assert r.status_code == 401


def test_identity_from_percent_encoded_header(client):
    """HTTP 头放不了原始中文，X-User 与 cookie 一样要先解码。"""
    from urllib.parse import quote

    pid = make_project(client)
    r = client.post(
        "/api/files",
        files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        data={"project_id": pid},
        headers={"X-User": quote("陈杰")},
    )
    assert r.status_code == 201
    assert r.json()["uploader_name"] == "陈杰"


def test_idempotent_reupload_same_content(client):
    set_identity(client, "张三")
    pid = make_project(client)
    f1 = upload(client, pid, "report.txt", b"same content")
    f2 = upload(client, pid, "report copy.txt", b"same content")
    assert f1["id"] == f2["id"]
    assert client.get("/api/files", params={"project_id": pid}).json()["total"] == 1


def test_team_snapshot_survives_reassignment(client):
    set_identity(client, "张三")
    team_a = make_team(client, "数据组")
    team_b = make_team(client, "算法组")
    pid = make_project(client)

    # 上传时张三在数据组
    r = client.post("/api/members", json={"name": "张三"})
    zhang = r.json()
    client.patch(f"/api/members/{zhang['id']}", json={"team_id": team_a})
    f = upload(client, pid, "数据字典.xlsx", b"fake xlsx bytes")
    assert f["team_name"] == "数据组"

    # 张三换到算法组后，历史文件仍归属数据组
    client.patch(f"/api/members/{zhang['id']}", json={"team_id": team_b})
    assert client.get("/api/files", params={"team_id": team_a}).json()["total"] == 1
    assert client.get("/api/files", params={"team_id": team_b}).json()["total"] == 0

    page = client.get(f"/teams/{team_a}")
    assert "数据字典.xlsx" in page.text


def test_eml_extractor_populates_title(client):
    set_identity(client, "王五")
    pid = make_project(client)
    f = upload(client, pid, "mail.eml", EML_BYTES)
    assert f["title"] == "三季度财报初稿"


def test_delete_removes_record_disk_and_fts(client):
    set_identity(client, "张三")
    pid = make_project(client)
    f = upload(client, pid, "temp.txt", b"to be deleted", notes="临时的")

    r = client.get("/api/search", params={"q": "临时"})
    assert r.json()["total"] == 1

    r = client.delete(f"/api/files/{f['id']}")
    assert r.status_code == 204

    assert client.get("/api/files", params={"project_id": pid}).json()["total"] == 0
    assert client.get("/api/search", params={"q": "临时"}).json()["total"] == 0

    settings = client.app.state.settings
    assert not any(settings.files_dir.rglob("*temp*"))
