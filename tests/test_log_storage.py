# tests/test_log_storage.py
from unittest.mock import patch
from fastapi.testclient import TestClient

import server
from server import _TOKEN

client = TestClient(server.app)
HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


# ── _save_logs ─────────────────────────────────────────────────────────────

def test_save_logs_writes_file(tmp_path):
    with patch.object(server, 'LOGS_DIR', str(tmp_path)):
        server._save_logs("horizon", "api-abc", "api", ["line1", "line2", "line3"])
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert content == "line1\nline2\nline3"
    assert "horizon__api-abc__api__" in files[0].name


def test_save_logs_empty_does_nothing(tmp_path):
    with patch.object(server, 'LOGS_DIR', str(tmp_path)):
        server._save_logs("horizon", "api-abc", "api", [])
    assert list(tmp_path.iterdir()) == []


# ── /api/saved-logs ────────────────────────────────────────────────────────

def test_list_saved_logs_sorted_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    (tmp_path / "ns__pod__ctr__2026-10-02T10-00-00.log").write_text("a")
    (tmp_path / "ns__pod__ctr__2026-10-02T11-00-00.log").write_text("b")
    r = client.get("/api/saved-logs", headers=HEADERS)
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 2
    assert "11-00-00" in files[0]["name"]   # newest first
    assert "10-00-00" in files[1]["name"]


def test_list_saved_logs_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["files"] == []


def test_list_saved_logs_unauthorized():
    r = client.get("/api/saved-logs")
    assert r.status_code == 401


# ── /api/saved-logs/download ───────────────────────────────────────────────

def test_download_valid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    fname = "ns__pod__ctr__2026-10-02T10-00-00.log"
    (tmp_path / fname).write_text("hello logs", encoding="utf-8")
    r = client.get(f"/api/saved-logs/download?file={fname}", headers=HEADERS)
    assert r.status_code == 200
    assert r.text == "hello logs"
    assert "attachment" in r.headers["content-disposition"]


def test_download_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs/download?file=../etc/passwd", headers=HEADERS)
    assert r.status_code == 400


def test_download_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'LOGS_DIR', str(tmp_path))
    r = client.get("/api/saved-logs/download?file=nonexistent.log", headers=HEADERS)
    assert r.status_code == 404


def test_download_unauthorized():
    r = client.get("/api/saved-logs/download?file=something.log")
    assert r.status_code == 401
