"""Database paths under synced roots are refused; tmp_path is allowed."""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from ledger import SyncPathError, SyncPathWarning, open_ledger, resolve_db_path


def test_tmp_path_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    lg = open_ledger(path)
    assert lg.path == path.resolve()
    lg.close()
    assert path.exists()


def test_dropbox_component_refused(tmp_path: Path) -> None:
    path = tmp_path / "Dropbox" / "ledger.db"
    with pytest.raises(SyncPathError, match="Dropbox"):
        open_ledger(path)
    with pytest.raises(SyncPathError, match="Dropbox"):
        resolve_db_path(path)
    assert not path.exists()


def test_icloud_mobile_documents_refused(tmp_path: Path) -> None:
    path = tmp_path / "Library" / "Mobile Documents" / "ledger.db"
    with pytest.raises(SyncPathError, match="iCloud"):
        open_ledger(path)
    assert not path.exists()


def test_syncthing_stfolder_refused(tmp_path: Path) -> None:
    root = tmp_path / "sync"
    root.mkdir()
    (root / ".stfolder").write_text("")
    path = root / "nested" / "ledger.db"
    with pytest.raises(SyncPathError, match="Syncthing"):
        open_ledger(path)
    assert not path.exists()


def test_dropbox_marker_dir_refused(tmp_path: Path) -> None:
    root = tmp_path / "cloud"
    root.mkdir()
    (root / ".dropbox").mkdir()
    path = root / "ledger.db"
    with pytest.raises(SyncPathError, match="Dropbox"):
        open_ledger(path)


def test_cloudstorage_onedrive_path_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "Library" / "CloudStorage" / "OneDrive-Personal" / "ledger.db"
    with pytest.raises(SyncPathError) as exc_info:
        open_ledger(path)
    assert "OneDrive-Personal" in str(exc_info.value)
    assert "~/.local/share/ledger" in str(exc_info.value)
    with pytest.raises(SyncPathError) as exc_info:
        resolve_db_path(path)
    assert "OneDrive-Personal" in str(exc_info.value)
    assert "~/.local/share/ledger" in str(exc_info.value)
    assert not path.exists()


def test_cloudstorage_other_provider_path_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-user@domain.com" / "ledger.db"
    with pytest.raises(SyncPathError) as exc_info:
        open_ledger(path)
    assert "GoogleDrive-user@domain.com" in str(exc_info.value)
    assert "~/.local/share/ledger" in str(exc_info.value)
    with pytest.raises(SyncPathError) as exc_info:
        resolve_db_path(path)
    assert "GoogleDrive-user@domain.com" in str(exc_info.value)
    assert "~/.local/share/ledger" in str(exc_info.value)
    assert not path.exists()


def test_desktop_path_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "Desktop" / "ledger.db"
    with pytest.warns(SyncPathWarning) as record:
        resolved = resolve_db_path(path)
    assert resolved == path.resolve()
    warn_text = str(record[0].message)
    assert "Desktop" in warn_text
    assert "iCloud Desktop & Documents" in warn_text
    assert "~/.local/share/ledger" in warn_text
    with pytest.warns(SyncPathWarning):
        lg = open_ledger(path)
        lg.close()
    assert path.exists()


def test_documents_path_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "Documents" / "ledger.db"
    with pytest.warns(SyncPathWarning) as record:
        resolved = resolve_db_path(path)
    assert resolved == path.resolve()
    warn_text = str(record[0].message)
    assert "Documents" in warn_text
    assert "iCloud Desktop & Documents" in warn_text
    assert "~/.local/share/ledger" in warn_text
    with pytest.warns(SyncPathWarning):
        lg = open_ledger(path)
        lg.close()
    assert path.exists()


def test_non_synced_path_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".local" / "share" / "ledger" / "ledger.db"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolved = resolve_db_path(path)
        assert resolved == path.resolve()
        lg = open_ledger(path)
        assert lg.path == path.resolve()
        lg.close()
    assert path.exists()


def test_cloudstorage_case_alias_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    prov_dir = tmp_path / "Library" / "CloudStorage" / "Prov"
    prov_dir.mkdir(parents=True)
    path = tmp_path / "library" / "cloudstorage" / "Prov" / "x.db"

    real_samefile = os.path.samefile

    def mock_samefile(p1: os.PathLike | str, p2: os.PathLike | str) -> bool:
        try:
            return real_samefile(p1, p2)
        except OSError:
            return str(Path(p1).resolve()).lower() == str(Path(p2).resolve()).lower()

    with patch("os.path.samefile", side_effect=mock_samefile):
        with pytest.raises(SyncPathError) as exc_info:
            open_ledger(path)
        assert "Prov" in str(exc_info.value)
        assert "~/.local/share/ledger" in str(exc_info.value)
        with pytest.raises(SyncPathError) as exc_info:
            resolve_db_path(path)
        assert "Prov" in str(exc_info.value)
        assert "~/.local/share/ledger" in str(exc_info.value)
        assert not path.exists()


def test_cloudstorage_outside_home_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    outside_dir = tmp_path / "outside" / "Library" / "CloudStorage" / "Prov"
    outside_dir.mkdir(parents=True)
    path = outside_dir / "x.db"
    resolved = resolve_db_path(path)
    assert resolved == path.resolve()
    lg = open_ledger(path)
    assert lg.path == path.resolve()
    lg.close()
    assert path.exists()


def test_desktop_case_alias_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir(parents=True)
    path = tmp_path / "desktop" / "ledger.db"

    real_samefile = os.path.samefile

    def mock_samefile(p1: os.PathLike | str, p2: os.PathLike | str) -> bool:
        try:
            return real_samefile(p1, p2)
        except OSError:
            return str(Path(p1).resolve()).lower() == str(Path(p2).resolve()).lower()

    with patch("os.path.samefile", side_effect=mock_samefile):
        with pytest.warns(SyncPathWarning) as record:
            resolved = resolve_db_path(path)
        assert len(record) == 1
        assert resolved == path.resolve()
        warn_text = str(record[0].message)
        assert "Desktop" in warn_text
        assert "iCloud Desktop & Documents" in warn_text
        assert "~/.local/share/ledger" in warn_text
        with pytest.warns(SyncPathWarning) as record:
            lg = open_ledger(path)
            lg.close()
        assert len(record) == 1
        assert path.exists()


def test_documents_case_alias_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    documents_dir = tmp_path / "Documents"
    documents_dir.mkdir(parents=True)
    path = tmp_path / "documents" / "ledger.db"

    real_samefile = os.path.samefile

    def mock_samefile(p1: os.PathLike | str, p2: os.PathLike | str) -> bool:
        try:
            return real_samefile(p1, p2)
        except OSError:
            return str(Path(p1).resolve()).lower() == str(Path(p2).resolve()).lower()

    with patch("os.path.samefile", side_effect=mock_samefile):
        with pytest.warns(SyncPathWarning) as record:
            resolved = resolve_db_path(path)
        assert len(record) == 1
        assert resolved == path.resolve()
        warn_text = str(record[0].message)
        assert "Documents" in warn_text
        assert "iCloud Desktop & Documents" in warn_text
        assert "~/.local/share/ledger" in warn_text
        with pytest.warns(SyncPathWarning) as record:
            lg = open_ledger(path)
            lg.close()
        assert len(record) == 1
        assert path.exists()
