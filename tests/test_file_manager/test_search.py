"""元数据搜索语义：FTS+CJK 折叠、模糊兜底、放宽规则、matched 标注、模式注册。"""

from __future__ import annotations

from .conftest import EML_BYTES, make_project, set_identity, upload


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


def test_search_modes_registered(client):
    modes = {
        m["name"]: m["available"] for m in client.get("/api/modes").json()["modes"]
    }
    assert modes["metadata"] is True
    assert modes["fulltext"] is False
    assert modes["semantic"] is False

    r = client.get("/api/search", params={"q": "x", "mode": "semantic"})
    assert r.status_code == 501
