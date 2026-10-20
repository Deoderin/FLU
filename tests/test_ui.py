"""Локальный UI: состояние, запуск команд и защита от чужих запросов."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from fleet.ui import UiServer, build_state


@pytest.fixture
def ui(provisioned, root):
    server = UiServer(("127.0.0.1", 0), root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, path, body=None, host=None, header=True):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    req.add_header("Host", host or f"127.0.0.1:{port}")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
        if header:
            req.add_header("X-Fleet", "1")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_state(provisioned, root):
    state = build_state(root)
    assert state["errors"] == []
    robot = next(r for r in state["robots"] if r["id"] == 1)
    assert robot["provisioned"] and robot["slot"] == 1
    assert robot["mavlink"] == "tcp:10.150.0.1:5761"
    assert "build/robots/flu-001/robot.rsc" in robot["files"]
    acme = next(s for s in state["sites"] if s["name"] == "flu-acme")
    assert acme["has_key"] and acme["panel"] == "http://10.150.0.1:8080"
    external = next(r for r in state["robots"] if r["id"] == 3)
    assert external["mavlink"] is None  # площадка заказчика: арбитра нет


def test_page_and_files(ui):
    status, body = call(ui, "/")
    assert status == 200 and b"FLU fleet" in body
    status, body = call(ui, "/api/file?path=build/robots/flu-001/robot.rsc")
    assert status == 200 and b"fleet: flu-001" in body
    assert call(ui, "/api/file?path=secrets/robots/1.json")[0] == 404  # секреты не отдаём
    assert call(ui, "/api/file?path=build/../secrets/robots/1.json")[0] == 404


def test_foreign_requests_rejected(ui):
    assert call(ui, "/api/state", host="evil.example:80")[0] == 403  # DNS rebinding
    assert call(ui, "/api/run", {"args": ["check"]}, header=False)[0] == 403  # без X-Fleet
    assert call(ui, "/api/run", {"args": ["rm", "-rf"]})[0] == 400  # только команды fleet


def test_run_command(ui):
    status, body = call(ui, "/api/run", {"args": ["check"]})
    assert status == 200
    job = json.loads(body)
    deadline = time.time() + 30
    while job["running"] and time.time() < deadline:
        time.sleep(0.2)
        job = json.loads(call(ui, f"/api/jobs/{job['id']}")[1])
    assert job["code"] == 0
    assert "inventory в порядке" in job["output"]
