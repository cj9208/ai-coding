from __future__ import annotations

import pytest

from photo_desk.config import PhotoRootError, load_settings


def test_defaults_anchor_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTO_ROOT", raising=False)
    monkeypatch.delenv("PHOTO_DESK_DATA_DIR", raising=False)
    settings = load_settings()
    assert str(settings.data_dir).replace("\\", "/").endswith("data/photo_desk")
    assert settings.db_path.name == "photo_desk.db"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PHOTO_ROOT", str(tmp_path / "nas"))
    monkeypatch.setenv("PHOTO_DESK_DATA_DIR", str(tmp_path / "out"))
    settings = load_settings()
    assert settings.root == tmp_path / "nas"
    assert settings.data_dir == tmp_path / "out"


def test_rel_abs_roundtrip(tmp_path) -> None:
    settings = load_settings(tmp_path / "root", tmp_path / "out")
    rel = "2025/春/IMG_0001.jpg"
    path = settings.photo_path(rel)
    assert path == tmp_path / "root" / "2025" / "春" / "IMG_0001.jpg"
    (path).parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    assert settings.rel_of(path) == rel


def test_ensure_root_raises_when_missing(tmp_path) -> None:
    settings = load_settings(tmp_path / "unmounted", tmp_path / "out")
    with pytest.raises(PhotoRootError) as exc:
        settings.ensure_root()
    assert "PHOTO_ROOT" in str(exc.value)
