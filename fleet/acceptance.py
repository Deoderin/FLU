"""Приёмочный тест робота: всё ли настроено и работает, одним отчётом.

Запускается с инженерной станции, подключённой к WireGuard площадки
(оператор test-bench) и, если нужно, к сервисному туннелю.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import addressing as A
from . import fc
from .inventory import Inventory, Robot
from .keystore import SecretStore
from .mikrotik import connect_robot, drift
from .params import diff
from .render import Renderer

FAILSAFE_PARAMS = {"FS_GCS_ENABLE", "FS_GCS_TIMEOUT", "FS_ACTION", "SYSID_MYGCS", "SYSID_ENFORCE", "MAV_GCS_SYSID", "MAV_OPTIONS"}
MAV_TYPE_GROUND_ROVER = 10


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


def rtsp_describe(url: str, timeout: float = 25) -> tuple[bool, str]:
    """RTSP DESCRIBE без ffmpeg. MediaMTX сам дёргает камеру (sourceOnDemand) и отвечает 200 или 404."""
    u = urlparse(url)
    with socket.create_connection((u.hostname, u.port or 554), timeout=timeout) as s:
        s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: fleet\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    status = data.split(b"\r\n", 1)[0].decode(errors="replace")
    return " 200 " in status, status


class Acceptance:
    def __init__(self, inv: Inventory, store: SecretStore, robot: Robot, via: str = "auto", mikrotik: bool = True):
        self.inv, self.store, self.robot = inv, store, robot
        self.via, self.mikrotik = via, mikrotik
        self.checks: list[Check] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    def guarded(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # одна упавшая проверка не должна прятать остальные
            self.add(name, "FAIL", f"{type(exc).__name__}: {exc}")

    def run(self) -> list[Check]:
        robot = self.robot
        errors = self.inv.validate()
        self.add("inventory", "FAIL" if errors else "PASS", "; ".join(errors[:3]))
        if not robot.provisioned:
            self.add("прошивка", "FAIL", f"робот не прошит: fleet provision --id {robot.id}")
            return self.checks
        self.site = self.inv.site(robot.site)
        self.renderer = Renderer(self.inv, self.store)
        _, self.fingerprint = self.renderer.robot_rsc(robot, self.site, robot.slot)

        if self.site.managed:
            self.server = str(A.server_ip(self.site))
            self.reachable = False
            self.guarded("арбитр", self.check_arbiter)
            if self.reachable:
                self.guarded("MAVLink", self.check_mavlink)
                self.guarded("камеры", self.check_cameras)
            else:
                for name in ("MAVLink", "камеры"):
                    self.add(name, "SKIP", f"сервер площадки {self.server} недоступен с этой станции")
        else:
            for name in ("арбитр", "MAVLink", "камеры"):
                self.add(name, "SKIP", f"площадка заказчика {self.site.name}: проверяется на их стороне")

        if self.mikrotik:
            self.guarded("MikroTik", self.check_mikrotik)
        else:
            self.add("MikroTik", "SKIP", "отключено флагом --no-mikrotik")
        return self.checks

    # --- проверки ------------------------------------------------------------

    def check_arbiter(self) -> None:
        url = f"http://{self.server}:{A.PANEL_PORT}/api/state"
        try:
            state = requests.get(url, timeout=5).json()
        except requests.RequestException as exc:
            self.add("арбитр", "FAIL", f"{url} недоступен ({exc.__class__.__name__}): включён ли WireGuard площадки на этой станции?")
            return
        self.reachable = True
        status = next((r for r in state.get("robots", []) if r["name"] == self.robot.name), None)
        if status is None:
            self.add("арбитр", "FAIL", f"{self.robot.name} нет в конфиге арбитра: fleet deploy-server --site {self.site.name}")
        elif not status["robot_online"]:
            self.add("арбитр", "FAIL", "арбитр не может подключиться к ФК робота (туннель, netmap, адаптер ФК)")
        else:
            self.add("арбитр", "PASS", f"робот на связи, владелец: {status['owner'] or 'нет'}")

    def check_mavlink(self) -> None:
        robot, slot = self.robot, self.robot.slot
        conn = f"tcp:{self.server}:{A.arbiter_port(slot)}"
        m, hb = fc.connect(conn)
        try:
            sysid = hb.get_srcSystem()
            if sysid != slot:
                self.add("MAVLink", "FAIL", f"SYSID {sysid}, ожидался {slot}: fleet fc --id {robot.id} --connect ... --push")
            elif hb.type != MAV_TYPE_GROUND_ROVER:
                self.add("MAVLink", "WARN", f"тип аппарата {hb.type}, ожидался ground rover ({MAV_TYPE_GROUND_ROVER})")
            else:
                self.add("MAVLink", "PASS", f"heartbeat от SYSID {sysid} через {conn}")

            actual, _ = fc.fetch_params(m)
            mismatch = diff(self.renderer.expected_params(robot), actual)
            if not mismatch:
                self.add("параметры", "PASS", f"{len(actual)} параметров, расхождений с эталоном нет")
            else:
                shown = ", ".join(f"{n}={a} (нужно {e:g})" for n, e, a in mismatch[:10])
                bad_fs = sorted({n for n, _, _ in mismatch} & FAILSAFE_PARAMS)
                prefix = f"FAILSAFE: {', '.join(bad_fs)}. " if bad_fs else ""
                self.add("параметры", "FAIL", f"{prefix}{len(mismatch)} расхождений: {shown}")

            tel = fc.telemetry(m)
            fix = tel.get("gps_fix")
            self.add("GPS", "PASS" if fix and fix >= 3 else "WARN",
                     f"fix {fix}, спутников {tel.get('satellites')}" if fix is not None else "нет GPS_RAW_INT")
            battery = self.inv.platform(robot).get("battery", {})
            v = tel.get("battery_v")
            if v is None:
                self.add("батарея", "WARN", "напряжение не пришло")
            else:
                ok = battery.get("min_v", 0) <= v <= battery.get("max_v", 1e9)
                self.add("батарея", "PASS" if ok else "WARN", f"{v:.1f} В")
        finally:
            m.close()

    def check_cameras(self) -> None:
        for cam in A.CAMERAS:
            url = f"rtsp://{self.server}:{A.RTSP_PORT}/{self.robot.name}-{cam}"
            try:
                ok, status = rtsp_describe(url)
            except OSError as exc:
                ok, status = False, f"{exc.__class__.__name__}: {exc}"
            self.add(f"камера {cam}", "PASS" if ok else "FAIL", f"{url}: {status}")

    def check_mikrotik(self) -> None:
        robot = self.robot
        with connect_robot(self.inv, self.store, robot, via=self.via) as mt:
            got = mt.fingerprint()
            if got == self.fingerprint:
                self.add("MikroTik: конфиг", "PASS", f"отпечаток {got}")
            else:
                self.add("MikroTik: конфиг", "FAIL",
                         f"отпечаток {got}, ожидался {self.fingerprint}: конфиг устарел — перепрошейте (fleet bench mikrotik)")

            age = mt.last_handshake(f"fleet-site:{self.site.name}")
            self.add("туннель площадки", "PASS" if age is not None and age < 180 else "FAIL",
                     f"handshake {age} с назад" if age is not None else "handshake не было")

            baseline = self.renderer.robot_dir(robot) / "baseline.rsc"
            if baseline.exists():
                changes = drift(baseline.read_text("utf-8"), mt.export())
                self.add("MikroTik: дрейф", "PASS" if not changes else "WARN",
                         "совпадает с эталоном" if not changes else f"{len(changes)} строк изменено: " + "; ".join(changes[:5]))
            else:
                self.add("MikroTik: дрейф", "SKIP", "нет эталона baseline.rsc (снимается при fleet bench mikrotik)")

            wan = self.inv.platform(robot)["wan"]
            if wan.get("type") == "lte":
                out = mt.run(f"/interface lte monitor {wan['interface']} once", check=False)
                signal = " ".join(line.strip() for line in out.splitlines() if line.strip().split(":")[0] in ("rsrp", "rsrq", "sinr"))
                self.add("LTE", "PASS" if signal else "WARN", signal or "нет данных о сигнале")


def save_report(root: Path, robot: Robot, checks: list[Check]) -> Path:
    path = root / "build" / "reports" / f"{robot.name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2), "utf-8")
    return path
