"""文件管理器端到端测试：上传管道、幂等、团队快照、搜索、删除。"""

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
        data={"project_id": project_id, **fields},
    )
    assert r.status_code == 201, r.text
    return r.json()


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


def test_metadata_search(client):
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "财报.eml", EML_BYTES, tags="财务, 三季度")
    upload(client, pid, "readme.txt", b"nothing here")

    r = client.get("/api/search", params={"q": "财报"})
    assert r.json()["total"] == 1
    assert r.json()["hits"][0]["filename"] == "财报.eml"

    r = client.get("/api/search", params={"q": "三季度"})
    assert r.json()["total"] == 1  # 标签命中

    # CJK 按相邻双字匹配：非相邻/不成词的组合不命中
    assert client.get("/api/search", params={"q": "季财"}).json()["total"] == 0
    assert client.get("/api/search", params={"q": "月报"}).json()["total"] == 0

    # 结构化筛选 + 关键词组合
    r = client.get("/api/search", params={"q": "财报", "extension": "pdf"})
    assert r.json()["total"] == 0


def test_search_matched_field_tags(client):
    """每条命中应标注来源字段：标题/文件名/备注/标签。"""
    set_identity(client, "李四")
    pid = make_project(client)
    # EML 主题为“三季度财报初稿”；文件名、备注、标签各不相同
    upload(client, pid, "report.eml", EML_BYTES, notes="季度汇总", tags="归档, 三季度")

    r = client.get("/api/search", params={"q": "财报"})  # 命中标题
    hit = r.json()["hits"][0]
    assert hit["matched"] == ["标题"]

    r = client.get("/api/search", params={"q": "report"})  # 命中文件名
    assert r.json()["hits"][0]["matched"] == ["文件名"]

    r = client.get("/api/search", params={"q": "汇总"})  # 命中备注
    assert r.json()["hits"][0]["matched"] == ["备注"]

    r = client.get("/api/search", params={"q": "归档"})  # 命中标签
    assert r.json()["hits"][0]["matched"] == ["标签"]

    # 纯筛选、无关键词：matched 为空，由页面显示为“筛选条件”
    r = client.get("/api/search", params={"extension": "eml"})
    assert r.json()["total"] == 1
    assert r.json()["hits"][0]["matched"] == []


def test_search_infix_substring_matches(client):
    """中缀兜底：词内部的片段也能命中，并标成"·模糊"以区别于词命中。"""
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "MonthlyReport-2026Q3.txt", b"body", notes="给管理层的季度汇总")

    def probe(q):
        d = client.get("/api/search", params={"q": q}).json()
        return d["total"], d["hits"][0]["matched"] if d["hits"] else []

    # ASCII：FTS 只有前缀通配，中缀靠 LIKE 兜底
    assert probe("Monthly") == (1, ["文件名"])
    total, matched = probe("eport")
    assert total == 1 and matched == ["文件名·模糊"]
    assert probe("026Q3")[0] == 1

    # 中文：相邻子串命中；不相邻的拼字仍然是 miss，避免把模糊变成噪音
    assert probe("度汇总")[0] == 1
    assert probe("季财报")[0] == 0


def test_search_is_case_insensitive(client):
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "MonthlyReport.txt", b"body")

    totals = {
        client.get("/api/search", params={"q": q}).json()["total"]
        for q in ["monthly", "MONTHLY", "MoNtHlY", "report", "REPORT"]
    }
    assert totals == {1}


def test_multiword_search_relaxes_only_when_strict_is_empty(client):
    """多词先按"全部命中"，为空才放宽为"任一命中"，并用 relaxed 告知前端。"""
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "report.eml", EML_BYTES, notes="季度汇总", tags="归档, 三季度")
    upload(client, pid, "budget.txt", b"x", notes="预算说明")

    strict = client.get("/api/search", params={"q": "归档 三季度"}).json()
    assert strict["relaxed"] is False and strict["total"] == 1

    relaxed = client.get("/api/search", params={"q": "归档 预算"}).json()
    assert relaxed["relaxed"] is True and relaxed["total"] == 2

    # 单个词没有可放宽的余地：保持空结果
    miss = client.get("/api/search", params={"q": "不存在xyz"}).json()
    assert miss["relaxed"] is False and miss["total"] == 0


def test_relaxed_search_still_requires_whole_word(client):
    """放宽只放宽词之间的关系：每个词内部的折叠单元仍要全部命中。

    "财报"折叠成 财/财报/报；不加括号时 FTS 的优先级会让只含单个"报"字的
    文件被 OR 分支拽进来。
    """
    set_identity(client, "李四")
    pid = make_project(client)
    # 内容必须各不相同，否则按内容哈希的幂等去重会把它们并成一条记录
    upload(client, pid, "季度财报.docx", b"quarterly-1")
    upload(client, pid, "runbook.txt", b"ops-2", tags="运维")
    upload(client, pid, "AI竞品周报.md", b"radar-3")

    d = client.get("/api/search", params={"q": "财报 运维"}).json()
    assert d["relaxed"] is True
    names = {h["filename"] for h in d["hits"]}
    assert names == {"季度财报.docx", "runbook.txt"}


def test_matched_fields_attribute_across_columns(client):
    """两个词分别命中标题和标签时，两列都要单独标出，且不算"·模糊"。"""
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "report.eml", EML_BYTES, tags="归档")

    d = client.get("/api/search", params={"q": "财报 归档"}).json()
    assert d["relaxed"] is False and d["total"] == 1
    assert d["hits"][0]["matched"] == ["标题", "标签"]


def test_search_page_shows_relaxed_hint(client):
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "report.eml", EML_BYTES, tags="归档")
    upload(client, pid, "budget.txt", b"x", notes="预算说明")

    page = client.get("/search", params={"q": "归档 预算"})
    assert "已放宽为任一命中" in page.text
    assert "report.eml" in page.text and "budget.txt" in page.text

    page = client.get("/search", params={"q": "eport"})
    assert "match-tag fuzzy" in page.text and "文件名·模糊" in page.text

    page = client.get("/search", params={"q": "归档"})
    assert "已放宽为任一命中" not in page.text


def test_form_submit_sends_empty_filter_values(client):
    """浏览器提交整个表单：没选的下拉框是空串，不能变成 422 JSON 页。"""
    set_identity(client, "李四")
    pid = make_project(client)
    tid = make_team(client)
    upload(client, pid, "coach-notes.txt", b"coach", tags="培训")

    form_data = {
        "q": "coach",
        "mode": "metadata",
        "project_id": "",
        "team_id": "",
        "uploader_id": "",
        "extension": "",
        "sort": "created_at_desc",
    }
    r = client.get("/", params=form_data)
    assert r.status_code == 200, r.text
    assert "coach-notes.txt" in r.text
    assert "int_parsing" not in r.text

    # 乱填的非数字值同样按"没填"处理
    assert (
        client.get("/", params={**form_data, "uploader_id": "abc"}).status_code == 200
    )
    assert (
        client.get(
            f"/projects/{pid}", params={"team_id": "", "uploader_id": ""}
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/teams/{tid}", params={"member_id": "", "project_id": ""}
        ).status_code
        == 200
    )

    # 成员表单里"（不归属）"同样是空串
    r = client.post(
        "/members", data={"name": "新同事", "team_id": "", "next": "/teams"}
    )
    assert r.status_code in (200, 303), r.text
    new_id = next(
        m["id"] for m in client.get("/api/members").json() if m["name"] == "新同事"
    )
    assert client.post(
        f"/members/{new_id}/assign", data={"team_id": "", "next": f"/teams/{tid}"}
    ).status_code in (200, 303)


def test_search_page_renders_tags_and_empty_state(client):
    set_identity(client, "李四")
    pid = make_project(client)
    upload(client, pid, "财报.eml", EML_BYTES, tags="财务, 三季度")

    page = client.get("/search", params={"q": "财报"})
    assert page.status_code == 200
    assert "match-tag" in page.text and "标题" in page.text
    assert "匹配来源" in page.text  # 带关键词时才多出这一列

    page = client.get("/search", params={"q": "不存在的东西"})
    assert "没有匹配的文件" in page.text


def test_browse_and_search_share_one_page(client):
    """合并后：进去就是全部文件，不填关键词不应出现“匹配来源”列。"""
    set_identity(client, "赵六")
    pid = make_project(client)
    upload(client, pid, "会议纪要.txt", "本周进展".encode("utf-8"))
    upload(client, pid, "财报.eml", EML_BYTES)

    page = client.get("/")
    assert page.status_code == 200
    assert "会议纪要.txt" in page.text and "财报.eml" in page.text
    assert "匹配来源" not in page.text
    assert "共 2 个" in page.text


def test_sorting_applies_to_browse_and_api(client):
    set_identity(client, "赵六")
    pid = make_project(client)
    b = upload(client, pid, "b_report.txt", b"bb")
    a = upload(client, pid, "a_report.txt", b"a")
    c = upload(client, pid, "c_report.txt", b"ccc")

    def api_ids(sort):
        rows = client.get("/api/files", params={"sort": sort}).json()["files"]
        return [r["id"] for r in rows]

    assert api_ids("filename_asc") == [a["id"], b["id"], c["id"]]
    assert api_ids("filename_desc") == [c["id"], b["id"], a["id"]]
    assert api_ids("size_desc") == [c["id"], b["id"], a["id"]]
    # 未知取值不报错，回退到创建时间新→旧（同秒并列时按 id 倒序，故只断言首个）
    assert api_ids("bogus")[0] == c["id"]

    page = client.get("/", params={"sort": "filename_asc"})
    assert 'value="filename_asc" selected' in page.text.replace("\n", " ")


def test_pager_keeps_filters_and_sort(client):
    """分页链接必须带上当前条件，否则翻到第二页筛选就丢。"""
    set_identity(client, "赵六")
    pid = make_project(client)
    for i in range(51):
        upload(client, pid, f"file{i}.txt", f"content {i}".encode("utf-8"))

    page = client.get("/search", params={"q": "file", "sort": "filename_asc"})
    assert "下一页" in page.text and "q=file" in page.text
    import re

    href = re.search(
        r'<a [^>]*href="([^"]+)">下一页', page.text.replace("&amp;", "&")
    ).group(1)
    assert "q=file" in href and "sort=filename_asc" in href and "page=2" in href

    page2 = client.get(
        "/search", params={"q": "file", "sort": "filename_asc", "page": 2}
    )
    assert "上一页" in page2.text and "page=1" in page2.text


def test_team_page_adds_member_via_dropdown(client):
    """团队页只能下拉选择已有成员加入团队，不能就地创建成员。"""
    set_identity(client, "王五")
    team_a = make_team(client, "数据组")
    team_b = make_team(client, "算法组")
    client.post("/api/members", json={"name": "甲"})
    b = client.post("/api/members", json={"name": "乙"}).json()
    c = client.post("/api/members", json={"name": "丙"}).json()
    client.patch(f"/api/members/{b['id']}", json={"team_id": team_b})
    client.patch(f"/api/members/{c['id']}", json={"team_id": team_a})

    # 团队 A 的添加成员下拉框：不含已在 A 的丙，含未归属的甲和归属 B 的乙
    page = client.get(f"/teams/{team_a}")
    assert page.status_code == 200
    import re

    form = re.search(
        rf'action="/teams/{team_a}/add-member".*?</form>', page.text, re.S
    ).group(0)
    options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', form)
    names = [name for _, name in options]
    assert "甲" in names and "乙" in names and "丙" not in names

    # 下拉提交：把乙加入团队 A（TestClient 默认跟随 303 重定向，故 200 也算成功）
    r = client.post(f"/teams/{team_a}/add-member", data={"member_id": b["id"]})
    assert r.status_code in (200, 303)
    members = client.get("/api/members").json()
    assert any(m["id"] == b["id"] and m["team_id"] == team_a for m in members)

    # 不存在的成员不能借此创建：404，成员总数不变
    total_before = len(client.get("/api/members").json())
    r = client.post(f"/teams/{team_a}/add-member", data={"member_id": 9999})
    assert r.status_code == 404
    assert len(client.get("/api/members").json()) == total_before


def test_search_modes_registered(client):
    modes = {
        m["name"]: m["available"] for m in client.get("/api/modes").json()["modes"]
    }
    assert modes["metadata"] is True
    assert modes["fulltext"] is False
    assert modes["semantic"] is False

    r = client.get("/api/search", params={"q": "x", "mode": "semantic"})
    assert r.status_code == 501


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


def test_pages_smoke(client):
    set_identity(client, "张三")
    make_project(client)
    make_team(client)
    for path in ["/", "/projects", "/teams", "/search"]:
        r = client.get(path)
        assert r.status_code == 200, path
