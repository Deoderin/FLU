"""Простой локальный веб-интерфейс к fleet: `fleet ui`.

Показывает площадки и роботов, запускает команды fleet и показывает их вывод.
Слушает только 127.0.0.1: это интерфейс инженера на стенде, не операторов.
Команды выполняются тем же CLI в отдельном процессе, поэтому логика одна.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import addressing as A
from .inventory import Inventory, InventoryError
from .keystore import SecretError, SecretStore

PAGE = Path(__file__).with_name("ui.html")
PKG_PARENT = Path(__file__).resolve().parents[1]
COMMANDS = {"check", "render", "provision", "bench", "fc", "deploy-server", "kit", "move", "test"}
MAX_OUTPUT = 200_000


class Job:
    """Команда fleet в отдельном процессе; вывод копится, пока она идёт."""

    def __init__(self, args: list[str], root: Path):
        self.id = uuid.uuid4().hex[:8]
        self.args = args
        self.output = ""
        self.code: int | None = None
        self.started = time.time()
        env = {**os.environ, "FLEET_ROOT": str(root), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fleet", *args], cwd=PKG_PARENT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.output = (self.output + line)[-MAX_OUTPUT:]
        self.code = self.proc.wait()

    @property
    def running(self) -> bool:
        return self.code is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": "fleet " + " ".join(self.args),
            "running": self.running,
            "code": self.code,
            "seconds": round(time.time() - self.started),
            "output": self.output,
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


def build_state(root: Path) -> dict:
    try:
        inv = Inventory.load(root)
    except InventoryError as exc:
        return {"root": str(root), "fatal": str(exc), "errors": [], "sites": [], "robots": []}
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
            "has_key": store.has_site(s.name) if s.managed else None,
            "operators": [o.name for o in inv.operators_on(s.name)],
            "panel": f"http://{A.server_ip(s)}:{A.PANEL_PORT}" if s.managed else None,
        })

    robots = []
    for r in sorted(inv.robots.values(), key=lambda r: r.id):
        rdir = root / "build" / "robots" / r.name
        site = inv.sites.get(r.site) if r.site else None
        robots.append({
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
            "mavlink": f"tcp:{A.server_ip(site)}:{A.arbiter_port(r.slot)}" if r.provisioned and site.managed else None,
            "report": latest_report(root, r.name),
        })

    service = None
    if inv.service:
        service = {"endpoint": str(inv.service.endpoint), "host": inv.service.host, "has_key": store.has_service()}

    return {
        "root": str(root),
        "errors": inv.validate(),
        "sites": sites,
        "robots": robots,
        "service": service,
        "ports": serial_ports(),
    }


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path):
        super().__init__(address, Handler)
        self.root = root
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.lock = threading.Lock()

    def start(self, args: list[str]) -> Job:
        with self.lock:
            if self.current and self.current.running:
                raise RuntimeError(f"уже выполняется: {self.current.to_dict()['command']}")
            job = Job(args, self.root)
            self.jobs[job.id] = job
            self.current = job
            return job


class Handler(BaseHTTPRequestHandler):
    server: UiServer

    def log_message(self, fmt, *args) -> None:
        pass  # без шума в консоли

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

    def _host_ok(self) -> bool:
        # Защита от DNS rebinding: чужой сайт, указавший своё имя на 127.0.0.1, не пройдёт
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(403, "доступ только с 127.0.0.1")
        url = urlparse(self.path)
        root = self.server.root
        if url.path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            state = build_state(root)
            state["job"] = self.server.current.to_dict() if self.server.current else None
            return self._json(state)
        if url.path.startswith("/api/jobs/"):
            job = self.server.jobs.get(url.path.rsplit("/", 1)[-1])
            return self._json(job.to_dict()) if job else self._error(404, "нет такой команды")
        if url.path == "/api/file":
            rel = parse_qs(url.query).get("path", [""])[0]
            build = (root / "build").resolve()
            path = (root / rel).resolve()
            if build not in path.parents or not path.is_file():
                return self._error(404, "показываются только файлы из build/")
            return self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
        if url.path == "/api/camera-password":
            try:
                robot_id = int(parse_qs(url.query).get("id", ["0"])[0])
                return self._json({"password": SecretStore(root).robot(robot_id)["camera_password"]})
            except (ValueError, SecretError) as exc:
                return self._error(404, str(exc))
        return self._error(404, "нет такой страницы")

    def do_POST(self) -> None:
        if not self._host_ok() or self.headers.get("X-Fleet") != "1":
            return self._error(403, "запрос отклонён")
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "некорректный JSON")
        if url.path == "/api/run":
            args = body.get("args")
            if not (isinstance(args, list) and args and all(isinstance(a, str) for a in args) and args[0] in COMMANDS):
                return self._error(400, "неизвестная команда")
            try:
                return self._json(self.server.start(args).to_dict())
            except RuntimeError as exc:
                return self._error(409, str(exc))
        if url.path == "/api/stop":
            job = self.server.current
            if job and job.running:
                job.proc.terminate()
            return self._json({"ok": True})
        return self._error(404, "нет такой страницы")


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> None:
    server = UiServer(("127.0.0.1", port), root)
    url = f"http://127.0.0.1:{port}/"
    print(f"fleet ui: {url}  (корень {root}; остановить — Ctrl+C)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
