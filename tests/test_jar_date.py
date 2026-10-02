# tests/test_jar_date.py
from unittest.mock import patch
from fastapi.testclient import TestClient

from server import app, _TOKEN

client = TestClient(app)
HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
PARAMS = {
    "context": "test-ctx",
    "namespace": "horizon",
    "pod": "api-abc123",
    "container": "api",
}


def test_jar_date_success():
    ls_output = "-rw-rw-r-- 1 root root 185101464 Sep 30 10:56 /app/app.jar\n"
    with patch("server.make_v1"), patch("server.kube_stream", return_value=ls_output):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "Sep 30 10:56"
    assert body["size_bytes"] == 185101464


def test_jar_date_no_such_file():
    ls_output = "ls: /app/app.jar: No such file or directory\n"
    with patch("server.make_v1"), patch("server.kube_stream", return_value=ls_output):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["date"] is None
    assert "error" in body


def test_jar_date_malformed_output():
    with patch("server.make_v1"), patch("server.kube_stream", return_value="weird\n"):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["date"] is None


def test_jar_date_exec_exception():
    with patch("server.make_v1"), \
         patch("server.kube_stream", side_effect=Exception("exec failed")):
        r = client.get("/api/jar-date", params=PARAMS, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["date"] is None


def test_jar_date_unauthorized():
    r = client.get("/api/jar-date", params=PARAMS)
    assert r.status_code == 401
