"""Route-level tests over a real inbox bundle: list, document, save, export."""

import pytest
from fastapi.testclient import TestClient

from ocr_review.app import create_app
from ocr_review.workspace import scan_inbox


@pytest.fixture
def client(bundle, tmp_path):
    app = create_app(bundle["dir"], tmp_path / "work")
    return TestClient(app)


@pytest.fixture
def ws_id(bundle):
    return scan_inbox(bundle["dir"])[0].ws_id


def test_index_lists_pending_bundle(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "scan" in page.text


def test_document_api_returns_contract_and_fresh_review(client, ws_id):
    data = client.get(f"/api/w/{ws_id}/document").json()

    assert data["review_is_new"] is True
    assert data["source_ok"] is True
    assert data["document"]["pages"][0]["blocks"][0]["content"].startswith("Machine")
    assert data["review"]["target"]["pipeline_version"] == "v1.6"


def test_save_roundtrip_and_conflict(client, ws_id):
    review = client.get(f"/api/w/{ws_id}/document").json()["review"]
    review["pages"] = [{"page_index": 0, "status": "done", "entries": {}}]

    ok = client.put(
        f"/api/w/{ws_id}/review",
        json={"review": review, "base_updated_at": None},
    )
    assert ok.status_code == 200

    stale = client.put(
        f"/api/w/{ws_id}/review",
        json={"review": review, "base_updated_at": "yesterday"},
    )
    assert stale.status_code == 409

    fresh = client.get(f"/api/w/{ws_id}/document").json()
    assert fresh["review_is_new"] is False
    assert fresh["review"]["pages"][0]["status"] == "done"


def test_save_stamps_identity_from_cookie(client, ws_id):
    client.post("/api/who", params={"name": "陈杰"})
    review = client.get(f"/api/w/{ws_id}/document").json()["review"]
    review["pages"] = [
        {
            "page_index": 0,
            "status": "pending",
            "entries": {
                "0": {"state": "corrected", "content": "x", "updated_by": None}
            },
        }
    ]
    client.put(f"/api/w/{ws_id}/review", json={"review": review})

    saved = client.get(f"/api/w/{ws_id}/document").json()["review"]
    entry = saved["pages"][0]["entries"]["0"]
    assert entry["updated_by"] == "陈杰"
    assert entry["updated_at"]


def test_page_image_served_and_path_traversal_rejected(client, ws_id):
    assert client.get(f"/w/{ws_id}/pages/page-000.png").status_code == 200
    assert client.get(f"/w/{ws_id}/pages/page-999.png").status_code == 404
    assert client.get(f"/w/{ws_id}/pages/..%2Fmeta.json").status_code == 404


def test_export_end_to_end(client, ws_id, tmp_path):
    review = client.get(f"/api/w/{ws_id}/document").json()["review"]
    review["pages"] = [
        {
            "page_index": 0,
            "status": "done",
            "entries": {
                "0": {"state": "corrected", "content": "人工修正", "updated_by": "r"}
            },
        }
    ]
    client.put(f"/api/w/{ws_id}/review", json={"review": review})

    out = client.post(f"/api/w/{ws_id}/export")
    assert out.status_code == 200
    json_path, md_path = out.json()["paths"]
    assert "人工修正" in open(json_path, encoding="utf-8").read()
    assert "人工修正" in open(md_path, encoding="utf-8").read()


def test_unknown_workspace_404(client):
    assert client.get("/api/w/deadbeefcafe/document").status_code == 404
    assert client.get("/w/deadbeefcafe").status_code == 404
