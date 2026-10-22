"""Локальний веб-інтерфейс до fleet: `fleet ui`.

/          — простий режим: пілот і НРК, команда для сервера, відповідь сервера,
             тунель пілота у WireGuard і прошивка роутера. Лише кнопки й «скопіювати-вставити».
/advanced  — розширений режим для інженера: майданчики, НРК і всі кроки.

Слухає лише 127.0.0.1. Довгі дії (WireGuard, роутер, тест) виконуються тим самим CLI
в окремому процесі, тож логіка одна. Мова (uk / en) приходить заголовком X-Lang.
"""
from __future__ import annotations

import base64
import binascii
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, __version__
from . import addressing as A
from .i18n import LANGS, T, get_lang, set_lang
from .inventory import Inventory, InventoryError
from .keystore import SecretError, SecretStore

PAGES = {"/": Path(__file__).with_name("simple.html"), "/advanced": Path(__file__).with_name("ui.html")}
PKG_PARENT = Path(__file__).resolve().parents[1]
COMMANDS = {"check", "render", "provision", "bench", "fc", "deploy-server", "kit", "move", "test", "wg-install"}
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_OUTPUT = 200_000


def mask_args(args: list[str]) -> list[str]:
    """Пароль роутера не потрапляє ні на екран, ні в журнал."""
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a == "--password":
            out[i + 1] = "***"
    return out


def log_dir(root: Path) -> Path:
    path = root / "build" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ui_log(root: Path, text: str) -> None:
    """Подія простого режиму в build/logs/ui.log: що натиснули і чим це закінчилось."""
    with (log_dir(root) / "ui.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")


class Job:
    """Команда fleet в окремому процесі; вивід накопичується, поки вона йде,
    і паралельно пишеться в журнал build/logs/<час>-<команда>.log."""

    def __init__(self, args: list[str], root: Path, lang: str):
        self.id = uuid.uuid4().hex[:8]
        self.args = args
        self.shown = "fleet " + " ".join(mask_args(args))
        self.output = ""
        self.code: int | None = None
        self.started = time.time()
        words = list(itertools.takewhile(lambda a: not a.startswith("-"), args))[:2]  # «bench mikrotik», без --id 1
        slug = re.sub(r"[^a-z0-9-]+", "-", "-".join(words).lower()).strip("-") or "fleet"
        self.log_path = log_dir(root) / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.log"
        self.log_rel = self.log_path.relative_to(root).as_posix()
        self._log = self.log_path.open("w", encoding="utf-8")
        self._log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  {self.shown}\n# fleet {__version__}, Python {sys.version.split()[0]}\n\n")
        env = {**os.environ, "FLEET_ROOT": str(root), "FLEET_LANG": lang,
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fleet", *args], cwd=PKG_PARENT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.output = (self.output + line)[-MAX_OUTPUT:]
            self._log.write(line)
            self._log.flush()
        self.code = self.proc.wait()
        self._log.write(f"\n# exit code {self.code}, {round(time.time() - self.started)} s\n")
        self._log.close()

    @property
    def running(self) -> bool:
        return self.code is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.shown,
            "running": self.running,
            "code": self.code,
            "seconds": round(time.time() - self.started),
            "output": self.output,
            "log": self.log_rel,
        }


def latest_report(root: Path, name: str) -> dict | None:
    reports = sorted((root / "build" / "reports").glob(f"{name}-*.json"))
    if not reports:
        return None
    path = reports[-1]
    checks = json.loads(path.read_text("utf-8"))
    stamp = path.stem[len(name) + 1:]  # 20261020-233930
    return {
        "time": f"{stamp[6:8]}.{stamp[4:6]}.{stamp[0:4]} {stamp[9:11]}:{stamp[11:13]}",
        "fail": sum(c["status"] == "FAIL" for c in checks),
        "warn": sum(c["status"] == "WARN" for c in checks),
        "checks": checks,
    }


def serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "description": p.description} for p in list_ports.comports()]


def robot_row(root: Path, inv: Inventory, r) -> dict:
    from .render import Renderer

    rdir = root / "build" / "robots" / r.name
    site = inv.sites.get(r.site) if r.site else None
    renderer = Renderer(inv, SecretStore(root))
    qgc = renderer.qgc_address(r) if site else None
    return {
        "id": r.id,
        "name": r.name,
        "serial": r.serial,
        "platform": r.platform,
        "site": r.site,
        "site_kind": site.kind if site else None,
        "slot": r.slot,
        "provisioned": r.provisioned,
        "service_tunnel": r.service_tunnel,
        "baseline": (rdir / "baseline.rsc").exists(),
        "files": sorted(p.relative_to(root).as_posix() for p in rdir.glob("*") if p.is_file()),
        "mavlink": f"tcp:{qgc}" if qgc else None,
        "qgc": qgc,
        "cameras": renderer.camera_streams(r) if site else [],
        "report": latest_report(root, r.name),
    }


def build_state(root: Path) -> dict:
    """Стан для розширеного режиму."""
    try:
        inv = Inventory.load(root)
    except InventoryError as exc:
        return {"root": str(root), "fatal": str(exc), "errors": [], "sites": [], "robots": [], "service": None, "ports": []}
    store = SecretStore(root)

    sites = []
    for s in inv.sites.values():
        sites.append({
            "name": s.name,
            "kind": s.kind,
            "customer": s.customer,
            "endpoint": str(s.endpoint),
            "host": s.host,
            "robots": len(inv.robots_on(s.name)),
            "capacity": A.capacity(s),
            "has_key": store.has_site(s.name) if s.managed else bool(s.server_pubkey) if s.hub else None,
            "operators": [o.name for o in inv.operators_on(s.name)],
            "panel": f"http://{A.server_ip(s)}:{A.PANEL_PORT}" if s.managed else None,
        })

    service = None
    if inv.service:
        service = {"endpoint": str(inv.service.endpoint), "host": inv.service.host, "has_key": store.has_service()}

    return {
        "root": str(root),
        "errors": inv.validate(),
        "sites": sites,
        "robots": [robot_row(root, inv, r) for r in sorted(inv.robots.values(), key=lambda r: r.id)],
        "service": service,
        "ports": serial_ports(),
    }


def simple_state(root: Path) -> dict:
    """Стан для простого режиму: hub-майданчик, пілоти цього ноутбука, НРК, тунелі пілотів."""
    from .hub import HubError, hub_site
    from .pilot import tunnel_name, tunnel_status, wireguard_installed

    try:
        inv = Inventory.load(root)
        site = hub_site(inv)
    except (InventoryError, HubError) as exc:
        return {"fatal": str(exc)}
    local = sorted(p.stem for p in (root / "secrets" / "operators").glob("*.json"))
    robots = [robot_row(root, inv, r) for r in sorted(inv.robots.values(), key=lambda r: r.id) if r.site == site.name]
    return {
        "site": site.name,
        "server": {"endpoint": str(site.endpoint), "host": site.host} if site.server_pubkey else None,
        "pilots": [o.name for o in inv.operators_on(site.name)],
        "pilot_ips": {o.name: str(A.operator_ip(site, o)) for o in inv.operators_on(site.name)},
        "local_pilots": local,
        "robots": robots,
        # У кожного пілота цього ноутбука свій тунель; працює щонайбільше один
        "tunnels": {p: {"name": tunnel_name(site.name, p), "status": tunnel_status(tunnel_name(site.name, p))} for p in local},
        "wireguard": wireguard_installed(),
        "errors": inv.validate(),
    }


def pilot_conf(root: Path, pilot: str) -> Path:
    """Конфіг тунелю пілота: технік віддає його на ноутбук пілота."""
    from .hub import hub_site
    from .inventory import NAME_RE

    if not NAME_RE.match(pilot):
        raise ValueError(T("некоректне ім'я пілота", "invalid pilot name"))
    path = root / "build" / "sites" / hub_site(Inventory.load(root)).name / "operators" / f"{pilot}.conf"
    if not path.exists():
        raise ValueError(T(f"конфігу пілота {pilot} ще немає: спочатку вставте відповідь сервера",
                           f"there is no config for pilot {pilot} yet: paste the server response first"))
    return path


MAX_KEY_BYTES = 64 * 1024

# Вікно ssh: одразу каже, куди підключаємося, і не висить мовчки, якщо порт 22 закритий.
# Лише ASCII: cmd ненадійно показує кирилицю.
SSH_CMD = """@echo off
title {app} - ssh {target}
cd /d "%USERPROFILE%"
echo Connecting to {target} ...
ssh -o ConnectTimeout=15 {key}{target}
if errorlevel 255 (
  echo.
  echo Could not connect to {host}. Check:
  echo  - the server is running and the address {host} is right;
  echo  - port 22 is open for this computer: AWS - EC2 - Security group - Inbound rules - SSH - My IP.
)
"""


def check_ssh_target(host: str, user: str) -> None:
    if not HOST_RE.match(host) or not USER_RE.match(user):
        raise ValueError(T("некоректна адреса сервера або ім'я користувача", "invalid server address or user name"))


def save_ssh_key(data: bytes, host: str, name: str = "") -> str:
    """AWS: ключ .pem сервера -> ~/.ssh/aws-<host>.pem; повертає шлях відносно домашньої теки.

    ssh у Windows відкидає ключ, до якого мають доступ інші користувачі (UNPROTECTED PRIVATE KEY FILE),
    а файл у ~/.ssh успадковує права лише власника. Ключ там і лишається: наступного разу для цього
    сервера вибирати його вже не треба. host перевірено (check_ssh_target), тож шлях безпечний.
    """
    if b"PuTTY-User-Key-File" in data:
        raise ValueError(T("це ключ PuTTY (.ppk), а потрібен .pem: у PuTTYgen — Conversions → Export OpenSSH key",
                           "this is a PuTTY key (.ppk), a .pem is needed: in PuTTYgen - Conversions -> Export OpenSSH key"))
    if len(data) > MAX_KEY_BYTES or b"PRIVATE KEY" not in data:
        name = name or T("файл", "the file")
        raise ValueError(T(f"{name} — не файл ключа .pem", f"{name} is not a .pem key file"))
    rel = f".ssh/aws-{host}.pem"
    dst = Path.home() / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return rel


def install_ssh_key(src: str, host: str) -> str:
    """Те саме для шляху до файлу ключа."""
    path = Path(src.strip().strip('"'))  # «Копіювати як шлях» у Провіднику додає лапки
    if not path.is_file():
        raise ValueError(T(f"файл ключа не знайдено: {path}", f"key file not found: {path}"))
    return save_ssh_key(path.read_bytes(), host, path.name)


def saved_ssh_key(host: str) -> str | None:
    """Ключ, збережений для цього сервера раніше."""
    rel = f".ssh/aws-{host}.pem"
    return rel if (Path.home() / rel).is_file() else None


def open_ssh_terminal(host: str, user: str, key: str | None = None) -> Path | None:
    """Нове вікно консолі з `ssh user@host`: пілот лише вводить пароль (або входить ключем AWS) і вставляє команду.

    key — шлях до ключа .pem відносно домашньої теки (save_ssh_key / saved_ssh_key).
    """
    check_ssh_target(host, user)
    target = f"{user}@{host}"
    if os.name != "nt":
        hint = f"ssh -i ~/{key} {target}" if key else f"ssh {target}"
        raise ValueError(T(f"відкрийте термінал і виконайте: {hint}", f"open a terminal and run: {hint}"))
    script = Path(tempfile.gettempdir()) / f"fleet-ssh-{host}.cmd"
    text = SSH_CMD.format(app=APP_NAME, target=target, host=host, key=f"-i {key} " if key else "")
    script.write_text(text.replace("\n", "\r\n"), encoding="ascii")
    # start "" — порожній заголовок вікна, далі сама команда
    subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/k", str(script)], cwd=str(Path.home()))
    return script


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    # У Windows SO_REUSEADDR дає другому fleet ui зайняти той самий порт, і дві копії ділять запити між собою
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], root: Path):
        super().__init__(address, Handler)
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.lock = threading.Lock()

    def start(self, args: list[str]) -> Job:
        with self.lock:
            if self.current and self.current.running:
                raise RuntimeError(T(f"уже виконується: {self.current.to_dict()['command']}",
                                     f"already running: {self.current.to_dict()['command']}"))
            job = Job(args, self.root, get_lang())
            self.jobs[job.id] = job
            self.current = job
            return job


class Handler(BaseHTTPRequestHandler):
    server: UiServer

    def log_message(self, fmt, *args) -> None:
        pass  # без шуму в консолі

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _prepare(self) -> bool:
        lang = self.headers.get("X-Lang")
        set_lang(lang if lang in LANGS else os.environ.get("FLEET_LANG"))
        # Захист від DNS rebinding: чужий сайт, що вказав своє ім'я на 127.0.0.1, не пройде
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._error(403, T("доступ лише з 127.0.0.1", "access from 127.0.0.1 only"))
            return False
        return True

    def _job(self) -> dict | None:
        return self.server.current.to_dict() if self.server.current else None

    def do_GET(self) -> None:
        if not self._prepare():
            return
        url = urlparse(self.path)
        root = self.server.root
        if url.path in PAGES:
            return self._send(200, PAGES[url.path].read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self._json({**build_state(root), "job": self._job()})
        if url.path == "/api/simple":
            return self._json({**simple_state(root), "job": self._job()})
        if url.path.startswith("/api/jobs/"):
            job = self.server.jobs.get(url.path.rsplit("/", 1)[-1])
            return self._json(job.to_dict()) if job else self._error(404, T("немає такої команди", "no such command"))
        if url.path == "/api/file":
            rel = parse_qs(url.query).get("path", [""])[0]
            build = (root / "build").resolve()
            path = (root / rel).resolve()
            if build not in path.parents or not path.is_file():
                return self._error(404, T("показуються лише файли з build/", "only files from build/ are shown"))
            return self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
        if url.path == "/api/camera-password":
            try:
                robot_id = int(parse_qs(url.query).get("id", ["0"])[0])
                return self._json({"password": SecretStore(root).robot(robot_id)["camera_password"]})
            except (ValueError, SecretError) as exc:
                return self._error(404, str(exc))
        return self._error(404, T("немає такої сторінки", "no such page"))

    def do_POST(self) -> None:
        if not self._prepare():
            return
        if self.headers.get("X-Fleet") != "1":
            return self._error(403, T("запит відхилено", "request rejected"))
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._error(400, T("некоректний JSON", "invalid JSON"))
        root = self.server.root

        if url.path == "/api/run":
            args = body.get("args")
            if not (isinstance(args, list) and args and all(isinstance(a, str) for a in args) and args[0] in COMMANDS):
                return self._error(400, T("невідома команда", "unknown command"))
            try:
                job = self.server.start(args)
            except RuntimeError as exc:
                return self._error(409, str(exc))
            ui_log(root, f"run: {job.shown} -> {job.log_rel}")
            return self._json(job.to_dict())
        if url.path == "/api/stop":
            job = self.server.current
            if job and job.running:
                job.proc.terminate()
            return self._json({"ok": True})

        # Простий режим: швидкі дії виконуються тут же, без окремого процесу; усе пишеться в ui.log
        from .hub import HubError, admin_command, apply_response, prepare
        action = url.path.rsplit("/", 1)[-1]
        try:
            if url.path == "/api/simple/prepare":
                pilot, serial = str(body.get("pilot", "")).strip(), str(body.get("serial", "")).strip() or None
                result = prepare(root, pilot, serial)
                ui_log(root, f"prepare: pilot={pilot} serial={serial} -> nrk id {result['robot_id']}")
                return self._json(result)
            if url.path == "/api/simple/apply":
                summary = apply_response(root, str(body.get("text", "")))
                ui_log(root, f"apply: {summary}")
                return self._json(summary)
            if url.path == "/api/simple/check-router":
                from .mndp import describe, discover
                from .netcheck import check_router_link
                link = check_router_link()
                routers = [{**r, "text": describe(r)} for r in discover(3.0)]
                ui_log(root, f"check-router: {link['status']} {link['message']} | routers: {[r['text'] for r in routers]}")
                return self._json({"link": link, "routers": routers})
            if url.path == "/api/simple/ssh":
                host, user = str(body.get("host", "")).strip(), str(body.get("user", "root")).strip() or "root"
                check_ssh_target(host, user)  # до збереження ключа: host іде в ім'я файлу
                if body.get("key_data"):  # файл, вибраний у майстрі
                    try:
                        data = base64.b64decode(str(body["key_data"]), validate=True)
                    except (binascii.Error, ValueError):
                        raise ValueError(T("файл ключа пошкоджено — виберіть його ще раз",
                                           "the key file is damaged - choose it again")) from None
                    key = save_ssh_key(data, host, str(body.get("key_name", "")))
                elif str(body.get("key", "")).strip():
                    key = install_ssh_key(str(body["key"]), host)
                else:
                    key = saved_ssh_key(host)
                open_ssh_terminal(host, user, key)
                ui_log(root, f"ssh: opened terminal {user}@{host}" + (" with .pem key (AWS)" if key else ""))
                return self._json({"ok": True, "key": bool(key)})
            if url.path == "/api/simple/admin-command":
                act, serial, pilot = (str(body.get(k) or "").strip() for k in ("action", "serial", "pilot"))
                cmd = admin_command(root, act, serial=serial or None, pilot=pilot or None)
                ui_log(root, f"admin-command: {act} serial={serial} pilot={pilot}")
                return self._json({"command": cmd})
            if url.path == "/api/simple/open-pilot-conf":
                path = pilot_conf(root, str(body.get("pilot", "")))
                if os.name == "nt":
                    subprocess.Popen(f'explorer /select,"{path}"')  # Провідник із виділеним файлом тунелю
                ui_log(root, f"open-pilot-conf: {path.relative_to(root)}")
                return self._json({"path": str(path.relative_to(root))})
            if url.path == "/api/open-logs":
                path = log_dir(root)
                if os.name == "nt":
                    os.startfile(path)  # Провідник із теки журналів
                return self._json({"path": str(path)})
        except (HubError, InventoryError, SecretError, ValueError) as exc:
            ui_log(root, f"{action}: ERROR {exc}")
            return self._error(400, str(exc))
        except Exception:
            import traceback
            ui_log(root, f"{action}: UNEXPECTED\n{traceback.format_exc()}")
            return self._error(500, T("неочікувана помилка — подробиці в журналі build/logs/ui.log",
                                      "unexpected error - details in the log build/logs/ui.log"))
        return self._error(404, T("немає такої сторінки", "no such page"))


def ui_running(url: str) -> bool:
    """На цьому порту вже відповідає fleet ui (наприклад, ярлик клацнули вдруге)."""
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return APP_NAME.encode() in r.read()
    except (OSError, ValueError):
        return False


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> int:
    url = f"http://127.0.0.1:{port}/"
    try:
        server = UiServer(("127.0.0.1", port), root)
    except OSError:
        if not ui_running(url):
            print(T(f"помилка: порт {port} зайнятий іншою програмою. Закрийте її або запустіть fleet ui --port {port + 1}",
                    f"error: port {port} is used by another program. Close it or run fleet ui --port {port + 1}"), file=sys.stderr)
            return 2
        print(T(f"fleet ui вже працює — відкриваю {url}", f"fleet ui is already running - opening {url}"), flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0
    print(T(f"fleet ui: {url}  (корінь {root}; зупинити — Ctrl+C)", f"fleet ui: {url}  (root {root}; stop with Ctrl+C)"), flush=True)
    print(T(f"Не закривайте це вікно: поки воно відкрите, працює {APP_NAME}.",
            f"Keep this window open: {APP_NAME} runs while it is open."), flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
