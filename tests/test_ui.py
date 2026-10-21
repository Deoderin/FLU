"""Локальний UI: стан, запуск команд, мова та захист від чужих запитів."""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fleet.ui import UiServer, build_state


@pytest.fixture
def ui(provisioned, root):
    server = UiServer(("127.0.0.1", 0), root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, path, body=None, host=None, header=True, lang="uk"):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    req.add_header("Host", host or f"127.0.0.1:{port}")
    req.add_header("X-Lang", lang)
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
    assert "build/robots/nrk-001/robot.rsc" in robot["files"]
    acme = next(s for s in state["sites"] if s["name"] == "flu-acme")
    assert acme["has_key"] and acme["panel"] == "http://10.150.0.1:8080"
    external = next(r for r in state["robots"] if r["id"] == 3)
    assert external["mavlink"] is None  # майданчик замовника: арбітра немає


def test_page_and_files(ui):
    status, body = call(ui, "/")
    assert status == 200 and "налаштування НРК".encode() in body  # простий режим
    status, body = call(ui, "/advanced")
    assert status == 200 and b"FLU fleet" in body
    status, body = call(ui, "/api/file?path=build/robots/nrk-001/robot.rsc")
    assert status == 200 and b"fleet: nrk-001" in body
    assert call(ui, "/api/file?path=secrets/robots/1.json")[0] == 404  # секрети не віддаємо
    assert call(ui, "/api/file?path=build/../secrets/robots/1.json")[0] == 404


def test_foreign_requests_rejected(ui):
    assert call(ui, "/api/state", host="evil.example:80")[0] == 403  # DNS rebinding
    assert call(ui, "/api/run", {"args": ["check"]}, header=False)[0] == 403  # без X-Fleet
    assert call(ui, "/api/run", {"args": ["rm", "-rf"]})[0] == 400  # лише команди fleet


def run_and_wait(ui, args, lang="uk"):
    status, body = call(ui, "/api/run", {"args": args}, lang=lang)
    assert status == 200
    job = json.loads(body)
    deadline = time.time() + 30
    while job["running"] and time.time() < deadline:
        time.sleep(0.2)
        job = json.loads(call(ui, f"/api/jobs/{job['id']}")[1])
    return job


def test_run_command(ui):
    job = run_and_wait(ui, ["check"])
    assert job["code"] == 0
    assert "інвентар у порядку" in job["output"]


@pytest.fixture
def simple_ui(tmp_path):
    """Простий режим на справжньому (порожньому) інвентарі репозиторію — як на новому ноутбуці."""
    import shutil

    from .conftest import REPO

    for name in ("inventory", "base", "templates", "server"):
        shutil.copytree(REPO / name, tmp_path / name)
    server = UiServer(("127.0.0.1", 0), tmp_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_simple_mode_prepare(simple_ui):
    state = json.loads(call(simple_ui, "/api/simple")[1])
    assert (state["site"], state["server"], state["robots"], state["pilots"]) == ("main", None, [], [])
    status, body = call(simple_ui, "/api/simple/prepare", {"pilot": "ivan", "serial": "FLU-2026-0001"})
    assert status == 200 and json.loads(body)["command"].startswith("echo H4sI")
    state = json.loads(call(simple_ui, "/api/simple")[1])
    assert state["local_pilots"] == ["ivan"] and state["robots"][0]["serial"] == "FLU-2026-0001"
    assert state["robots"][0]["provisioned"] is False  # відповіді сервера ще не було


def test_simple_mode_errors_are_readable(simple_ui):
    status, body = call(simple_ui, "/api/simple/prepare", {"pilot": "Іван", "serial": "FLU-1"})
    assert status == 400 and "ім'я пілота" in json.loads(body)["error"]
    status, body = call(simple_ui, "/api/simple/apply", {"text": "привіт"})
    assert status == 400 and "FLU1:" in json.loads(body)["error"]
    # адреса сервера з підстановкою команд не пройде далі перевірки
    status, body = call(simple_ui, "/api/simple/ssh", {"host": "1.2.3.4 & calc", "user": "root"})
    assert status == 400


def test_aws_key_copied_to_ssh_dir(tmp_path, monkeypatch):
    from fleet.ui import install_ssh_key

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    pem = tmp_path / "Downloads" / "flu key.pem"
    pem.parent.mkdir()
    pem.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n")
    # «Копіювати як шлях» у Провіднику дає шлях у лапках
    assert install_ssh_key(f'"{pem}"', "13.50.196.138") == ".ssh/flu-13.50.196.138.pem"
    assert (tmp_path / "home" / ".ssh" / "flu-13.50.196.138.pem").read_bytes() == pem.read_bytes()

    with pytest.raises(ValueError, match="не знайдено"):
        install_ssh_key(str(tmp_path / "nope.pem"), "13.50.196.138")
    ppk = tmp_path / "key.ppk"
    ppk.write_text("PuTTY-User-Key-File-3: ssh-rsa\n")
    with pytest.raises(ValueError, match="PuTTY"):
        install_ssh_key(str(ppk), "13.50.196.138")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    with pytest.raises(ValueError, match="не файл ключа"):
        install_ssh_key(str(notes), "13.50.196.138")


def test_ssh_terminal_with_aws_key(simple_ui, tmp_path, monkeypatch):
    import fleet.ui

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    started = []
    monkeypatch.setattr(fleet.ui.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(fleet.ui.os, "name", "nt")
    pem = tmp_path / "key.pem"
    pem.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    status, _ = call(simple_ui, "/api/simple/ssh", {"host": "13.50.196.138", "user": "ubuntu", "key": str(pem)})
    assert status == 200
    assert started[-1][-4:] == ["ssh", "-i", ".ssh/flu-13.50.196.138.pem", "ubuntu@13.50.196.138"]
    status, _ = call(simple_ui, "/api/simple/ssh", {"host": "13.50.196.138", "user": "root"})
    assert status == 200 and started[-1][-2:] == ["ssh", "root@13.50.196.138"]  # без ключа — як раніше
    status, body = call(simple_ui, "/api/simple/ssh", {"host": "13.50.196.138", "user": "ubuntu", "key": str(tmp_path / "x.pem")})
    assert status == 400 and "не знайдено" in json.loads(body)["error"]


def test_router_password_never_shown():
    from fleet.ui import mask_args

    assert mask_args(["bench", "mikrotik", "--id", "1", "--password", "secret"]) == \
        ["bench", "mikrotik", "--id", "1", "--password", "***"]


def test_job_writes_log(ui, root):
    job = run_and_wait(ui, ["check"])
    log = (root / job["log"]).read_text("utf-8")
    assert job["log"].startswith("build/logs/") and job["log"].endswith("-check.log")
    assert "# " in log and "fleet check" in log and "інвентар у порядку" in log and "# exit code 0" in log
    assert "run: fleet check" in (root / "build" / "logs" / "ui.log").read_text("utf-8")


def test_log_name_skips_options(ui):
    job = run_and_wait(ui, ["render", "--robot", "1"])
    assert job["log"].endswith("-render.log")  # не «render---robot.log»


def test_simple_actions_are_logged(simple_ui, monkeypatch):
    from fleet import mndp, netcheck

    monkeypatch.setattr(netcheck, "check_router_link", lambda adapters=None: {"status": "no_cable", "message": "кабель не під'єднано"})
    monkeypatch.setattr(mndp, "discover", lambda timeout=3.0: [])
    call(simple_ui, "/api/simple/prepare", {"pilot": "ivan", "serial": "FLU-2026-0001"})
    call(simple_ui, "/api/simple/apply", {"text": "не те"})
    status, body = call(simple_ui, "/api/simple/check-router", {})
    assert status == 200 and json.loads(body)["link"]["status"] == "no_cable"
    log = (simple_ui.root / "build" / "logs" / "ui.log").read_text("utf-8")
    assert "prepare: pilot=ivan serial=FLU-2026-0001" in log
    assert "apply: ERROR" in log and "check-router: no_cable" in log


def test_language_follows_request(ui):
    job = run_and_wait(ui, ["check"], lang="en")
    assert "inventory is fine" in job["output"]
    status, body = call(ui, "/api/file?path=secrets/x", lang="en")
    assert status == 404 and "only files from build/" in json.loads(body)["error"]


def test_second_launch_opens_running_ui(simple_ui, monkeypatch):
    import fleet.ui

    opened = []
    monkeypatch.setattr(fleet.ui.webbrowser, "open", opened.append)
    port = simple_ui.server_address[1]
    # ярлик клацнули вдруге: друга копія не стартує, лише відкривається браузер
    assert fleet.ui.serve(simple_ui.root, port=port) == 0
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_port_taken_by_another_program(tmp_path, capsys):
    import socket

    from fleet.ui import serve

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        assert serve(tmp_path, port=other.getsockname()[1], open_browser=False) == 2
    assert "зайнятий іншою програмою" in capsys.readouterr().err
