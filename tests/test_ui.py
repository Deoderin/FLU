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
    assert status == 200 and b"UGV Quick Setup" in body and "Пілот і НРК".encode() in body  # простий режим
    status, body = call(ui, "/advanced")
    assert status == 200 and b"UGV Quick Setup" in body and b"advanced" in body
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
    """Простий режим як на новому ноутбуці: лише шаблони інвентарю, inventory/ програма створить сама."""
    import shutil

    from .conftest import REPO

    for name in ("inventory.example", "base", "templates", "server"):
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
    assert install_ssh_key(f'"{pem}"', "203.0.113.10") == ".ssh/aws-203.0.113.10.pem"
    assert (tmp_path / "home" / ".ssh" / "aws-203.0.113.10.pem").read_bytes() == pem.read_bytes()

    with pytest.raises(ValueError, match="не знайдено"):
        install_ssh_key(str(tmp_path / "nope.pem"), "203.0.113.10")
    ppk = tmp_path / "key.ppk"
    ppk.write_text("PuTTY-User-Key-File-3: ssh-rsa\n")
    with pytest.raises(ValueError, match="PuTTY"):
        install_ssh_key(str(ppk), "203.0.113.10")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    with pytest.raises(ValueError, match="не файл ключа"):
        install_ssh_key(str(notes), "203.0.113.10")


def test_ssh_terminal_with_aws_key(simple_ui, tmp_path, monkeypatch):
    """Ключ вибирають у майстрі (вміст файлу), програма кладе його в ~/.ssh і далі бере сама."""
    import base64

    import fleet.ui

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(fleet.ui.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    started = []
    monkeypatch.setattr(fleet.ui.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(fleet.ui.os, "name", "nt")

    def ssh_line() -> str:
        script = Path(started[-1][-1]).read_text("ascii")
        assert "echo Connecting to" in script and "if errorlevel 255" in script  # вікно не мовчить
        return next(line for line in script.splitlines() if line.startswith("ssh "))

    pem = b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
    key = {"key_data": base64.b64encode(pem).decode(), "key_name": "flu.pem"}
    status, _ = call(simple_ui, "/api/simple/ssh", {"host": "203.0.113.10", "user": "ubuntu", **key})
    assert status == 200
    assert ssh_line() == "ssh -o ConnectTimeout=15 -i .ssh/aws-203.0.113.10.pem ubuntu@203.0.113.10"
    assert (tmp_path / "home" / ".ssh" / "aws-203.0.113.10.pem").read_bytes() == pem
    # наступного разу ключ вибирати не треба
    assert call(simple_ui, "/api/simple/ssh", {"host": "203.0.113.10", "user": "ubuntu"})[0] == 200
    assert ssh_line() == "ssh -o ConnectTimeout=15 -i .ssh/aws-203.0.113.10.pem ubuntu@203.0.113.10"
    # інший сервер без ключа — звичайний вхід за паролем
    assert call(simple_ui, "/api/simple/ssh", {"host": "198.51.100.7", "user": "root"})[0] == 200
    assert ssh_line() == "ssh -o ConnectTimeout=15 root@198.51.100.7"

    status, body = call(simple_ui, "/api/simple/ssh", {"host": "203.0.113.10", "user": "ubuntu",
                                                       "key_data": base64.b64encode(b"hello").decode(), "key_name": "notes.txt"})
    assert status == 400 and "notes.txt — не файл ключа" in json.loads(body)["error"]
    status, _ = call(simple_ui, "/api/simple/ssh", {"host": "../evil", "user": "ubuntu", **key})
    assert status == 400 and not (tmp_path / "home" / "evil.pem").exists()  # адресу перевірено до запису ключа


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


def test_each_local_pilot_has_own_tunnel(simple_ui, monkeypatch):
    import fleet.pilot
    import fleet.ui

    assert fleet.pilot.tunnel_name("main", "pilot-2") == "main-pilot-2"
    monkeypatch.setattr(fleet.pilot, "tunnel_status", lambda name: "running" if name == "main-ivan" else "missing")
    call(simple_ui, "/api/simple/prepare", {"pilot": "ivan", "serial": "FLU-2026-0001"})
    call(simple_ui, "/api/simple/prepare", {"pilot": "petro", "serial": "FLU-2026-0002"})
    state = json.loads(call(simple_ui, "/api/simple")[1])
    assert state["tunnels"] == {"ivan": {"name": "main-ivan", "status": "running"},
                                "petro": {"name": "main-petro", "status": "missing"}}

    # файл тунелю для ноутбука пілота: лише після відповіді сервера і лише для правильного імені
    opened = []
    monkeypatch.setattr(fleet.ui.subprocess, "Popen", opened.append)
    status, body = call(simple_ui, "/api/simple/open-pilot-conf", {"pilot": "ivan"})
    assert status == 400 and "ще немає" in json.loads(body)["error"]
    assert call(simple_ui, "/api/simple/open-pilot-conf", {"pilot": "../secrets"})[0] == 400
    conf = simple_ui.root / "build" / "sites" / "main" / "operators" / "ivan.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("[Interface]\n")
    status, body = call(simple_ui, "/api/simple/open-pilot-conf", {"pilot": "ivan"})
    assert status == 200 and json.loads(body)["path"] == str(Path("build/sites/main/operators/ivan.conf"))


def test_admin_command_from_wizard(simple_ui):
    status, body = call(simple_ui, "/api/simple/admin-command", {"action": "give", "serial": "PF-01", "pilot": "petro"})
    assert status == 200 and "bash /tmp/flu-hub.sh '--give' 'PF-01' 'petro'" in json.loads(body)["command"]
    status, body = call(simple_ui, "/api/simple/admin-command", {"action": "give", "serial": "ПФ-01", "pilot": "petro"})
    assert status == 400 and "кирилиця (ПФ)" in json.loads(body)["error"]
    assert call(simple_ui, "/api/simple/admin-command", {"action": "rm -rf", "serial": "PF-01"})[0] == 400


def test_termite_needs_registered_ugv(simple_ui):
    """Кнопка «Прописати камери в Termite» до реєстрації НРК на сервері пояснює, чого бракує."""
    call(simple_ui, "/api/simple/prepare", {"pilot": "ivan", "serial": "FLU-2026-0001"})
    status, body = call(simple_ui, "/api/simple/termite", {"id": 1})
    assert status == 400 and "спершу зареєструйте" in json.loads(body)["error"]


def test_fleet_command_for_jobs(monkeypatch, tmp_path):
    """Прошивку й тести вікно запускає окремим процесом: з .venv — python -m fleet, у зібраній програмі — fleet.exe."""
    import sys

    from fleet import ui

    monkeypatch.delattr(sys, "frozen", raising=False)
    pythonw = tmp_path / "pythonw.exe"
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(pythonw))
    assert ui.fleet_command() == [str(tmp_path / "python.exe"), "-m", "fleet"]  # pythonw без консолі — вивід не забрати
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "UGV Quick Setup.exe"))
    assert ui.fleet_command() == [str(tmp_path / "fleet.exe")]


def test_frozen_app_keeps_data_next_to_exe(monkeypatch, tmp_path):
    import sys

    import fleet

    monkeypatch.delenv("FLEET_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "UGV Quick Setup.exe"))
    assert fleet._root() == tmp_path.resolve()


def test_give_hidden_in_the_built_app(simple_ui, monkeypatch):
    import sys

    assert json.loads(call(simple_ui, "/api/simple")[1])["features"] == {"give": True}  # з репозиторію — є
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("FLEET_EXPERIMENTAL", raising=False)
    assert json.loads(call(simple_ui, "/api/simple")[1])["features"] == {"give": False}
