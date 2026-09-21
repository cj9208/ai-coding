"""页面层：浏览/搜索单页合一、排序、分页链接保参、表单空值、团队下拉。"""

from __future__ import annotations

from .conftest import (
    EML_BYTES,
    make_project,
    make_team,
    set_identity,
    upload,
)


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


def test_pages_smoke(client):
    set_identity(client, "张三")
    make_project(client)
    make_team(client)
    for path in ["/", "/projects", "/teams", "/search"]:
        r = client.get(path)
        assert r.status_code == 200, path
