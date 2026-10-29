"""Приймальний тест НРК: чи все налаштовано й працює, одним звітом.

Запускається з ноутбука, де ввімкнено тунель пілота у WireGuard майданчика
(і, за потреби, сервісний тунель).
У кожної перевірки є стабільний key (за ним UI малює статус кроків)
і назва мовою запуску.
"""
from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import addressing as A
from . import fc
from .i18n import T
from .inventory import Inventory, Robot
from .keystore import SecretStore
from .mikrotik import MAC_FIELD_RE, MikroTikAuthError, MikroTikError, connect_robot, drift
from .params import diff
from .render import Renderer

FAILSAFE_PARAMS = {"FS_GCS_ENABLE", "FS_GCS_TIMEOUT", "FS_ACTION", "SYSID_MYGCS", "SYSID_ENFORCE", "MAV_GCS_SYSID", "MAV_OPTIONS"}
MAV_TYPE_GROUND_ROVER = 10


def label(key: str) -> str:
    labels = {
        "inventory": T("інвентар", "inventory"),
        "registration": T("реєстрація", "registration"),
        "arbiter": T("арбітр", "arbiter"),
        "mavlink": "MAVLink",
        "params": T("параметри", "parameters"),
        "gps": "GPS",
        "battery": T("батарея", "battery"),
        "cam1": T("передня камера", "front camera"),
        "cam2": T("задня камера", "rear camera"),
        "cameras": T("камери", "cameras"),
        "dev_cam1": T("передня камера на борту", "front camera on board"),
        "dev_cam2": T("задня камера на борту", "rear camera on board"),
        "dev_fc": T("адаптер ФК на борту", "FC adapter on board"),
        "mikrotik": "MikroTik",
        "mikrotik_config": T("MikroTik: конфіг", "MikroTik: config"),
        "site_tunnel": T("тунель майданчика", "site tunnel"),
        "mikrotik_drift": T("MikroTik: дрейф", "MikroTik: drift"),
        "lte": "LTE",
    }
    return labels.get(key, key)


@dataclass
class Check:
    key: str
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


def rtsp_describe(url: str, timeout: float = 25) -> tuple[bool, str]:
    """RTSP DESCRIBE без ffmpeg. MediaMTX сам смикає камеру (sourceOnDemand) і відповідає 200 або 404."""
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

    def add(self, key: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(key, label(key), status, detail))

    def guarded(self, key: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # одна невдала перевірка не повинна ховати решту
            self.add(key, "FAIL", f"{type(exc).__name__}: {exc}")

    def run(self) -> list[Check]:
        robot = self.robot
        errors = self.inv.validate()
        self.add("inventory", "FAIL" if errors else "PASS", "; ".join(errors[:3]))
        if not robot.provisioned:
            self.add("registration", "FAIL", T(f"НРК не зареєстровано: fleet provision --id {robot.id}",
                                               f"the UGV is not registered: fleet provision --id {robot.id}"))
            return self.checks
        self.site = self.inv.site(robot.site)
        self.renderer = Renderer(self.inv, self.store)
        _, self.fingerprint = self.renderer.robot_rsc(robot, self.site, robot.slot)

        if self.site.managed:
            self.server = str(A.server_ip(self.site))
            self.reachable = False
            self.guarded("arbiter", self.check_arbiter)
            if self.reachable:
                self.guarded("mavlink", self.check_mavlink)
                self.guarded("cameras", self.check_cameras)
            else:
                for key in ("mavlink", "cameras"):
                    self.add(key, "SKIP", T(f"сервер майданчика {self.server} недоступний з цієї станції",
                                            f"the site server {self.server} is unreachable from this station"))
        elif self.site.hub:
            self.add("arbiter", "SKIP", T("на сервері лише WireGuard, арбітра немає", "the server runs WireGuard only, no arbiter"))
            for key in ("mavlink", "cameras"):
                self.add(key, "SKIP", T("пілот підключається до НРК напряму — перевірте в QGroundControl",
                                        "the pilot connects to the UGV directly - check in QGroundControl"))
        else:
            for key in ("arbiter", "mavlink", "cameras"):
                self.add(key, "SKIP", T(f"майданчик замовника {self.site.name}: перевіряється на їхньому боці",
                                        f"customer site {self.site.name}: checked on their side"))

        if self.mikrotik:
            self.guarded("mikrotik", self.check_mikrotik)
        else:
            self.add("mikrotik", "SKIP", T("вимкнено прапорцем --no-mikrotik", "disabled with --no-mikrotik"))
        return self.checks

    # --- перевірки -----------------------------------------------------------

    def check_arbiter(self) -> None:
        url = f"http://{self.server}:{A.PANEL_PORT}/api/state"
        try:
            state = requests.get(url, timeout=5).json()
        except requests.RequestException as exc:
            self.add("arbiter", "FAIL", T(
                f"{url} недоступний ({exc.__class__.__name__}): чи ввімкнено WireGuard майданчика на цій станції?",
                f"{url} is unreachable ({exc.__class__.__name__}): is the site WireGuard enabled on this station?"))
            return
        self.reachable = True
        status = next((r for r in state.get("robots", []) if r["name"] == self.robot.name), None)
        if status is None:
            self.add("arbiter", "FAIL", T(f"{self.robot.name} немає в конфігу арбітра: fleet deploy-server --site {self.site.name}",
                                          f"{self.robot.name} is not in the arbiter config: fleet deploy-server --site {self.site.name}"))
        elif not status["robot_online"]:
            self.add("arbiter", "FAIL", T("арбітр не може підключитися до ФК НРК (тунель, netmap, адаптер ФК)",
                                          "the arbiter cannot reach the UGV flight controller (tunnel, netmap, FC adapter)"))
        else:
            owner = status["owner"] or T("немає", "none")
            self.add("arbiter", "PASS", T(f"НРК на зв'язку, власник: {owner}", f"UGV online, owner: {owner}"))

    def check_mavlink(self) -> None:
        robot, slot = self.robot, self.robot.slot
        conn = f"tcp:{self.server}:{A.arbiter_port(slot)}"
        m, hb = fc.connect(conn)
        try:
            sysid = hb.get_srcSystem()
            if sysid != slot:
                self.add("mavlink", "FAIL", T(f"SYSID {sysid}, очікувався {slot}: fleet fc --id {robot.id} --connect ... --push",
                                              f"SYSID {sysid}, expected {slot}: fleet fc --id {robot.id} --connect ... --push"))
            elif hb.type != MAV_TYPE_GROUND_ROVER:
                self.add("mavlink", "WARN", T(f"тип апарата {hb.type}, очікувався ground rover ({MAV_TYPE_GROUND_ROVER})",
                                              f"vehicle type {hb.type}, expected ground rover ({MAV_TYPE_GROUND_ROVER})"))
            else:
                self.add("mavlink", "PASS", T(f"heartbeat від SYSID {sysid} через {conn}", f"heartbeat from SYSID {sysid} via {conn}"))

            actual, _ = fc.fetch_params(m)
            mismatch = diff(self.renderer.expected_params(robot), actual)
            if not mismatch:
                self.add("params", "PASS", T(f"{len(actual)} параметрів, розбіжностей з еталоном немає",
                                             f"{len(actual)} parameters, no differences from the reference"))
            else:
                shown = ", ".join(f"{n}={a} ({T('треба', 'need')} {e:g})" for n, e, a in mismatch[:10])
                bad_fs = sorted({n for n, _, _ in mismatch} & FAILSAFE_PARAMS)
                prefix = f"FAILSAFE: {', '.join(bad_fs)}. " if bad_fs else ""
                self.add("params", "FAIL", prefix + T(f"{len(mismatch)} розбіжностей: {shown}", f"{len(mismatch)} differences: {shown}"))

            tel = fc.telemetry(m)
            fix = tel.get("gps_fix")
            self.add("gps", "PASS" if fix and fix >= 3 else "WARN",
                     T(f"fix {fix}, супутників {tel.get('satellites')}", f"fix {fix}, satellites {tel.get('satellites')}")
                     if fix is not None else T("немає GPS_RAW_INT", "no GPS_RAW_INT"))
            battery = self.inv.platform(robot).get("battery", {})
            v = tel.get("battery_v")
            if v is None:
                self.add("battery", "WARN", T("напруга не прийшла", "no voltage reported"))
            else:
                ok = battery.get("min_v", 0) <= v <= battery.get("max_v", 1e9)
                self.add("battery", "PASS" if ok else "WARN", f"{v:.1f} V")
        finally:
            m.close()

    def check_cameras(self) -> None:
        for cam in A.CAMERAS:
            url = f"rtsp://{self.server}:{A.RTSP_PORT}/{self.robot.name}-{cam}"
            try:
                ok, status = rtsp_describe(url)
            except OSError as exc:
                ok, status = False, f"{exc.__class__.__name__}: {exc}"
            self.add(cam, "PASS" if ok else "FAIL", f"{url}: {status}")

    def check_mikrotik(self) -> None:
        robot = self.robot
        try:
            mt = connect_robot(self.inv, self.store, robot, via=self.via)
        except MikroTikAuthError:
            raise
        except MikroTikError as exc:
            if not self.site.hub:
                raise
            # hub: голий «timed out» нічого не каже — підказуємо, де шукати (і що видно на сервері)
            from . import netcheck

            lans, link = A.lan_block(self.site, robot.slot), netcheck.check_router_link()
            hint = T(
                f"{exc}. НРК не відповідає через тунель — найчастіше він не на зв'язку з сервером: НРК вимкнено; Starlink "
                f"не в порту 1 роутера чи без інтернету; роутер прошито версією до 0.3.4 з пристроєм у мережі 192.168.1.x "
                f"(перепрошийте кабелем, крок 5). На сервері: sudo wg show — у піра з {lans} мають бути endpoint і свіжий "
                f"latest handshake (немає й у пілотів — на сервері закрито UDP {self.site.endpoint.port})",
                f"{exc}. The UGV does not answer through the tunnel - most often it is not connected to the server: the UGV "
                f"is off; Starlink is not in router port 1 or has no internet; the router was flashed by a version before "
                f"0.3.4 with a device in 192.168.1.x (reflash with a cable, step 5). On the server: sudo wg show - the peer "
                f"with {lans} must have an endpoint and a fresh latest handshake (none for the pilots either - UDP "
                f"{self.site.endpoint.port} is closed on the server)")
            if link["status"] == "ok":  # поле 21.10, 24.10: DHCP роутера дав ноутбуку шлюз, і тунель пілота пішов у НРК
                hint += T(f". Ноутбук зараз під'єднано кабелем до роутера ({link['ip']}): кабель забирає в тунелю пілота "
                          f"інтернет — вийміть його й перевірте ще раз",
                          f". The laptop is now cabled to the router ({link['ip']}): the cable takes the internet away "
                          f"from the pilot tunnel - unplug it and check again")
            self.add("mikrotik", "FAIL", hint)
            return
        with mt:
            got = mt.fingerprint()
            if got == self.fingerprint:
                self.add("mikrotik_config", "PASS", T(f"відбиток {got}", f"fingerprint {got}"))
            else:
                self.add("mikrotik_config", "FAIL", T(
                    f"відбиток {got}, очікувався {self.fingerprint}: конфіг застарів — перепрошийте (fleet bench mikrotik)",
                    f"fingerprint {got}, expected {self.fingerprint}: the config is outdated - reflash (fleet bench mikrotik)"))

            self.check_devices(mt)

            age = mt.last_handshake(f"fleet-site:{self.site.name}")
            self.add("site_tunnel", "PASS" if age is not None and age < 180 else "FAIL",
                     T(f"handshake {age} с тому", f"handshake {age} s ago") if age is not None
                     else T("handshake не було", "no handshake yet"))

            baseline = self.renderer.robot_dir(robot) / "baseline.rsc"
            if baseline.exists():
                changes = drift(baseline.read_text("utf-8"), mt.export())
                self.add("mikrotik_drift", "PASS" if not changes else "WARN",
                         T("збігається з еталоном", "matches the reference") if not changes
                         else T(f"змінено рядків: {len(changes)}: ", f"{len(changes)} lines changed: ") + "; ".join(changes[:5]))
            else:
                self.add("mikrotik_drift", "SKIP", T("немає еталона baseline.rsc (знімається під час fleet bench mikrotik)",
                                                     "no baseline.rsc reference (taken by fleet bench mikrotik)"))

            wan = self.inv.platform(robot)["wan"]
            if wan.get("type") == "lte":
                out = mt.run(f"/interface lte monitor {wan['interface']} once", check=False)
                signal = " ".join(line.strip() for line in out.splitlines() if line.strip().split(":")[0] in ("rsrp", "rsrq", "sinr"))
                self.add("lte", "PASS" if signal else "WARN", signal or T("немає даних про сигнал", "no signal data"))

    def check_devices(self, mt) -> None:
        """Камери й адаптер ФК відповідають роутеру на тих адресах, куди він веде тунель.

        Саме цього бракувало: роутер прошився, тунель працює, а пристрій, якого прошивка не знайшла, стоїть на
        іншій адресі — і пілот бачить чорний екран. Пінгуємо з роутера; хто не відповідає на ping, але є в ARP,
        у мережі є (деякі адаптери ICMP ігнорують).
        """
        robot, ports = self.robot, self.inv.platform(self.robot)["ports"]
        for host in A.DEVICES:
            ip = str(A.device_ip(robot, host))
            outside = str(A.mapped_ip(self.site, robot.slot, host))
            out = mt.run(f"/ping {ip} count=3", check=False, timeout=30)
            received = re.findall(r"\breceived=(\d+)", out)
            port = ports.get(host, "?").removeprefix("ether")
            if received and int(received[-1]) > 0:
                self.add(f"dev_{host}", "PASS", T(f"{outside} → {ip} відповідає", f"{outside} -> {ip} answers"))
            elif MAC_FIELD_RE.search(mt.run(f"/ip arp print terse where address={ip}", check=False)):
                self.add(f"dev_{host}", "PASS", T(f"{outside} → {ip}: у мережі є (ARP), на ping не відповідає",
                                                  f"{outside} -> {ip}: on the network (ARP), does not answer ping"))
            else:
                self.add(f"dev_{host}", "FAIL", T(
                    f"{ip} не відповідає: перевірте живлення й кабель у порту {port}. Якщо пристрій на іншій адресі — "
                    f"перепрошийте роутер (крок 5): прошивка знайде його сама",
                    f"{ip} does not answer: check the power and the cable in port {port}. If the device is on another "
                    f"address - reflash the router (step 5): the flashing finds it itself"))


def save_report(root: Path, robot: Robot, checks: list[Check]) -> Path:
    path = root / "build" / "reports" / f"{robot.name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2), "utf-8")
    return path
