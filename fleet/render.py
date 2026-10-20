"""Сборка конфигов из inventory, секретов и шаблонов в build/.

build/robots/<robot>/        robot.rsc, robot.parm, expected.parm, move-*.rsc
build/sites/<site>/server/   бандл стека площадки для deploy-server
build/sites/<site>/operators/ конфиги WireGuard операторов и памятка с адресами
build/sites/<site>/kit/      интеграционный пакет external-площадки
build/service/               сервисный туннель: сервер и конфиги инженеров
"""
from __future__ import annotations

import csv
import hashlib
import io
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from ruamel.yaml import YAML

from . import addressing as A
from .inventory import Inventory, Robot, Site
from .keystore import SecretError, SecretStore, wg_pubkey
from .params import expected_params, format_parm, load_ignore, parse_parm, read_parm

MTU = 1380  # у LTE MTU меньше стандартного: без запаса фрагментация портит видео
MEDIAMTX_VERSION = "1.21.1"
SVC_LISTEN_PORT = 13230  # порт WireGuard сервисного туннеля на MikroTik
ARBITER_FILES = ("Dockerfile", "requirements.txt", "arbiter.py", "policy.py", "panel.html")
FP_PLACEHOLDER = "@FINGERPRINT@"


class RenderError(Exception):
    pass


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:  # LF: для bash и RouterOS
        f.write(text)
    return path


def ensure_ascii(text: str, what: str) -> None:
    """RouterOS плохо переносит UTF-8 в конфиге: ловим это при сборке, а не на роботе."""
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.isascii():
            raise RenderError(f"{what}, строка {lineno}: в конфиге MikroTik допустим только ASCII: {line!r}")


def dump_yaml(data: dict) -> str:
    y = YAML(typ="safe", pure=True)
    y.default_flow_style = False
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


class Renderer:
    def __init__(self, inv: Inventory, store: SecretStore):
        self.inv = inv
        self.store = store
        self.build = inv.root / "build"
        self.env = Environment(
            loader=FileSystemLoader(str(inv.root / "templates")),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def tpl(self, name: str, **ctx) -> str:
        return self.env.get_template(name).render(**ctx)

    def robot_dir(self, robot: Robot) -> Path:
        return self.build / "robots" / robot.name

    def site_dir(self, site: Site) -> Path:
        return self.build / "sites" / site.name

    def server_pubkey(self, site: Site) -> str:
        return wg_pubkey(self.store.site(site.name)["wg"]) if site.managed else site.server_pubkey

    # --- робот ---------------------------------------------------------------

    @staticmethod
    def lan_ctx() -> dict:
        return {"net": str(A.LAN), "mapped": str(A.LAN_MAPPED), **{h: str(A.lan_ip(h)) for h in A.HOSTS}}

    def site_ctx(self, robot: Robot, site: Site, slot: int, pending: bool = False) -> dict:
        tag = f"fleet-site:{site.name}" + (" pending" if pending else "")
        return {
            "name": site.name,
            "iface": site.iface,
            "tag": tag,
            "private_key": self.store.robot(robot.id)["wg_site"],
            "listen_port": site.robot_listen_port,
            "mtu": MTU,
            "server_pubkey": self.server_pubkey(site),
            "endpoint_host": site.endpoint.host,
            "endpoint_port": site.endpoint.port,
            "tunnel_net": str(site.network.tunnel),
            "robot_ip": str(A.robot_tunnel_ip(site, slot)),
            "prefix": site.network.tunnel.prefixlen,
            "lan_block": str(A.lan_block(site, slot)),
            "lan_mapped": str(A.LAN_MAPPED),
            "sources": [str(ip) for ip in site.sources()],
            "mgmt": [str(A.server_ip(site))] if site.managed else [],
        }

    def svc_ctx(self, robot: Robot) -> dict | None:
        if not robot.service_tunnel:
            return None
        svc = self.inv.service
        return {
            "iface": svc.iface,
            "private_key": self.store.robot(robot.id)["wg_svc"],
            "server_pubkey": wg_pubkey(self.store.service()["wg"]),
            "endpoint_host": svc.endpoint.host,
            "endpoint_port": svc.endpoint.port,
            "robot_ip": str(A.service_robot_ip(svc, robot.id)),
            "prefix": svc.network.prefixlen,
            "allowed": str(A.service_engineers_net(svc)),
            "listen_port": SVC_LISTEN_PORT,
            "mtu": MTU,
        }

    def _fingerprinted(self, template: str, ctx: dict) -> tuple[str, str]:
        draft = self.tpl(template, fingerprint=FP_PLACEHOLDER, **ctx)
        fp = hashlib.sha256(draft.encode()).hexdigest()[:12]
        text = draft.replace(FP_PLACEHOLDER, fp)
        ensure_ascii(text, template)
        return text, fp

    def robot_rsc(self, robot: Robot, site: Site, slot: int) -> tuple[str, str]:
        """Полный конфиг MikroTik и его отпечаток (по нему тест ловит устаревший конфиг)."""
        plat = self.inv.platform(robot)
        ctx = {
            "robot": robot,
            "platform": plat,
            "slot": slot,
            "lan": self.lan_ctx(),
            "mikrotik_password": self.store.robot(robot.id)["mikrotik_password"],
            "mav_port": plat["fc"]["mavlink_port"],
            "site": self.site_ctx(robot, site, slot),
            "svc": self.svc_ctx(robot),
        }
        return self._fingerprinted("mikrotik/robot.rsc.j2", ctx)

    def robot_params(self, robot: Robot, site_name: str, slot: int) -> tuple[str, dict[str, float]]:
        plat = self.inv.platform(robot)
        text = self.tpl(
            "ardupilot/robot.parm.j2",
            robot=robot,
            site_name=site_name,
            slot=slot,
            fc=plat["fc"],
            failsafe=plat["failsafe"],
            lan=self.lan_ctx(),
        )
        return text, parse_parm(text)

    def expected_params(self, robot: Robot) -> dict[str, float]:
        """Эталон параметров робота: база платформы без калибровок + параметры fleet."""
        base_name = self.inv.platform(robot)["fc"].get("base_params", "ardupilot-base.parm")
        base = read_parm(self.inv.root / "base" / base_name)
        patterns = load_ignore(self.inv.root / "base" / "ardupilot-ignore.txt")
        _, overrides = self.robot_params(robot, robot.site, robot.slot)
        return expected_params(base, overrides, patterns)

    def render_robot(self, robot: Robot) -> list[Path]:
        if not robot.provisioned:
            raise RenderError(f"{robot.name} ещё не прошит: fleet provision --id {robot.id}")
        site = self.inv.site(robot.site)
        rsc, _ = self.robot_rsc(robot, site, robot.slot)
        parm, _ = self.robot_params(robot, site.name, robot.slot)
        expected = format_parm(
            self.expected_params(robot),
            header=f"Expected parameters of {robot.name}: base + fleet overrides, calibration excluded",
        )
        out = self.robot_dir(robot)
        return [write(out / "robot.rsc", rsc), write(out / "robot.parm", parm), write(out / "expected.parm", expected)]

    def render_move(self, robot: Robot, new_site: Site, new_slot: int, rollback_min: int) -> tuple[Path, Path]:
        """Скрипты переезда: добавить новую площадку (с автооткатом) и подтвердить переезд."""
        new = self.site_ctx(robot, new_site, new_slot, pending=True)
        add = self.tpl("mikrotik/move-add.rsc.j2", robot=robot, old_site=robot.site, new=new, rollback_min=rollback_min)
        _, fp = self.robot_rsc(robot, new_site, new_slot)
        commit = self.tpl(
            "mikrotik/move-commit.rsc.j2",
            robot=robot,
            old_site=robot.site,
            new_site=new_site.name,
            pending_tag=new["tag"],
            final_tag=f"fleet-site:{new_site.name}",
            old_tag=f"fleet-site:{robot.site}",
            fingerprint=fp,
        )
        ensure_ascii(add, "move-add.rsc")
        ensure_ascii(commit, "move-commit.rsc")
        out = self.robot_dir(robot)
        return write(out / f"move-{new_site.name}-add.rsc", add), write(out / f"move-{new_site.name}-commit.rsc", commit)

    # --- площадка ------------------------------------------------------------

    def _robot_rows(self, site: Site) -> list[dict]:
        rows = []
        for r in self.inv.robots_on(site.name):
            sec = self.store.robot(r.id)
            plat = self.inv.platform(r)
            rows.append({
                "robot": r,
                "name": r.name,
                "id": r.id,
                "slot": r.slot,
                "public_key": wg_pubkey(sec["wg_site"]),
                "tunnel_ip": str(A.robot_tunnel_ip(site, r.slot)),
                "lan_block": str(A.lan_block(site, r.slot)),
                "fc_ip": str(A.mapped_ip(site, r.slot, "fc")),
                "cam1_ip": str(A.mapped_ip(site, r.slot, "cam1")),
                "cam2_ip": str(A.mapped_ip(site, r.slot, "cam2")),
                "mav_port": plat["fc"]["mavlink_port"],
                "rtsp_user": plat["cameras"]["rtsp_user"],
                "rtsp_path": plat["cameras"]["rtsp_path"],
                "camera_password": sec["camera_password"],
                "hold_mode": plat["modes"]["hold"],
            })
        return rows

    def render_site(self, site: Site) -> list[Path]:
        if not site.managed:
            raise RenderError(f"площадка {site.name} — external: для неё fleet kit")
        sip = str(A.server_ip(site))
        robots = self._robot_rows(site)
        out = self.site_dir(site)
        paths: list[Path] = []

        operators = []
        for o in self.inv.operators_on(site.name):
            key = self.store.operator(o.name)["wg"]
            ip = str(A.operator_ip(site, o))
            operators.append({"name": o.name, "role": o.role, "ip": ip, "public_key": wg_pubkey(key)})
            paths.append(write(out / "operators" / f"{o.name}.conf", self.tpl(
                "server/client.conf.j2",
                title=f"operator {o.name}, site {site.name}",
                private_key=key,
                address=ip,
                mtu=MTU,
                server_pubkey=self.server_pubkey(site),
                endpoint=str(site.endpoint),
                allowed=f"{sip}/32",
            )))
        paths.append(write(out / "operators" / "README.md", self._operators_memo(site, robots)))

        server = out / "server"
        paths.append(write(server / f"{site.iface}.conf", self.tpl(
            "server/wg-site.conf.j2",
            site=site,
            server_ip=sip,
            prefix=site.network.tunnel.prefixlen,
            private_key=self.store.site(site.name)["wg"],
            mtu=MTU,
            robots=robots,
            operators=operators,
        )))
        cameras = [
            {
                "path": f"{r['name']}-{cam}",
                "source": f"rtsp://{r['rtsp_user']}:{r['camera_password']}@{r[cam + '_ip']}:554{r['rtsp_path']}",
            }
            for r in robots
            for cam in A.CAMERAS
        ]
        paths.append(write(server / "config" / "mediamtx.yml", self.tpl(
            "server/mediamtx.yml.j2",
            site=site,
            server_ip=sip,
            operators_net=str(site.network.operators),
            rtsp_port=A.RTSP_PORT,
            webrtc_port=A.WEBRTC_PORT,
            cameras=cameras,
        )))
        paths.append(write(server / "config" / "arbiter.yaml", dump_yaml({
            "site": site.name,
            "bind": sip,
            "panel_port": A.PANEL_PORT,
            "policy": {"stop_from_any": True, "hold_on_handover": True, "handover_timeout_s": 120},
            "robots": [
                {"name": r["name"], "slot": r["slot"], "host": r["fc_ip"], "port": r["mav_port"],
                 "listen": A.arbiter_port(r["slot"]), "hold_mode": r["hold_mode"]}
                for r in robots
            ],
            "operators": [{"ip": o["ip"], "name": o["name"], "role": o["role"]} for o in operators],
        })))
        paths.append(write(server / "docker-compose.yml", self.tpl(
            "server/docker-compose.yml.j2", site=site, server_ip=sip, mediamtx_version=MEDIAMTX_VERSION)))
        paths.append(write(server / "install.sh", self.tpl(
            "server/install.sh.j2", title=f"site {site.name}", iface=site.iface, with_stack=True)))

        src = self.inv.root / "server" / "arbiter"
        for name in ARBITER_FILES:
            dst = server / "arbiter" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / name, dst)
            paths.append(dst)
        return paths

    def _operators_memo(self, site: Site, robots: list[dict]) -> str:
        sip = A.server_ip(site)
        lines = [
            f"# Площадка {site.name}: подключение операторов",
            "",
            "1. Импортируйте свой конфиг из этой папки в WireGuard и включите туннель.",
            f"2. Панель управления (кто владеет роботом, передача): http://{sip}:{A.PANEL_PORT}",
            "3. В QGroundControl / Mission Planner добавьте TCP-подключение к нужному роботу.",
            "   MAVLink system ID наземной станции оставьте 255 (по умолчанию).",
            "",
            "| Робот | MAVLink (TCP) | Камера 1 | Камера 2 |",
            "|---|---|---|---|",
        ]
        for r in robots:
            cams = [
                f"rtsp://{sip}:{A.RTSP_PORT}/{r['name']}-{c} (WebRTC: http://{sip}:{A.WEBRTC_PORT}/{r['name']}-{c})"
                for c in A.CAMERAS
            ]
            lines.append(f"| {r['name']} | {sip}:{A.arbiter_port(r['slot'])} | {cams[0]} | {cams[1]} |")
        return "\n".join(lines) + "\n"

    def render_kit(self, site: Site) -> list[Path]:
        """Интеграционный пакет для администратора сервера заказчика (external)."""
        robots = self._robot_rows(site)
        out = self.site_dir(site) / "kit"
        plat_failsafe = {self.inv.platform(r["robot"])["failsafe"]["gcs_timeout_s"] for r in robots} or {5}
        readme = self.tpl(
            "integration/README.md.j2",
            site=site,
            server_ip=str(A.server_ip(site)),
            robots=robots,
            mtu=MTU,
            failsafe_s=max(plat_failsafe),
        )
        peers = [f"# fleet: robots of site {site.name} - add to the WireGuard server config"]
        for r in robots:
            peers += ["", f"# robot {r['name']} (id {r['id']})", "[Peer]",
                      f"PublicKey = {r['public_key']}", f"AllowedIPs = {r['tunnel_ip']}/32, {r['lan_block']}"]
        creds = io.StringIO()
        w = csv.writer(creds, lineterminator="\n")
        w.writerow(["robot", "camera", "rtsp_url", "user", "password"])
        for r in robots:
            for cam in A.CAMERAS:
                w.writerow([r["name"], cam, f"rtsp://{r[cam + '_ip']}:554{r['rtsp_path']}", r["rtsp_user"], r["camera_password"]])
        return [
            write(out / "README.md", readme),
            write(out / "peers.conf", "\n".join(peers) + "\n"),
            write(out / "credentials.csv", creds.getvalue()),
        ]

    # --- сервисный туннель ---------------------------------------------------

    def render_service(self) -> list[Path]:
        svc = self.inv.service
        if svc is None:
            raise RenderError("раздел service в inventory/sites.yaml не задан")
        sip = str(A.service_server_ip(svc))
        server_pub = wg_pubkey(self.store.service()["wg"])
        out = self.build / "service"
        paths = []

        engineers = []
        for eng in svc.engineers:
            key = self.store.engineer(eng.name)["wg"]
            ip = str(A.service_engineer_ip(svc, eng))
            engineers.append({"name": eng.name, "ip": ip, "public_key": wg_pubkey(key)})
            paths.append(write(out / "engineers" / f"{eng.name}.conf", self.tpl(
                "server/client.conf.j2",
                title=f"engineer {eng.name}, service tunnel",
                private_key=key,
                address=ip,
                mtu=MTU,
                server_pubkey=server_pub,
                endpoint=str(svc.endpoint),
                allowed=str(svc.network),
            )))

        robots = []
        for r in sorted(self.inv.robots.values(), key=lambda r: r.id):
            if not r.service_tunnel:
                continue
            try:
                key = self.store.robot(r.id)["wg_svc"]
            except SecretError:
                continue  # ещё не прошит
            robots.append({"name": r.name, "id": r.id, "ip": str(A.service_robot_ip(svc, r.id)), "public_key": wg_pubkey(key)})

        paths.append(write(out / "server" / f"{svc.iface}.conf", self.tpl(
            "server/wg-svc.conf.j2",
            server_ip=sip,
            prefix=svc.network.prefixlen,
            port=svc.endpoint.port,
            private_key=self.store.service()["wg"],
            mtu=MTU,
            engineers_net=str(A.service_engineers_net(svc)),
            engineers=engineers,
            robots=robots,
        )))
        paths.append(write(out / "server" / "install.sh", self.tpl(
            "server/install.sh.j2", title="service tunnel hub", iface=svc.iface, with_stack=False)))
        return paths

    # --- всё сразу -----------------------------------------------------------

    def render_all(self) -> tuple[list[Path], list[str]]:
        paths: list[Path] = []
        notes: list[str] = []
        for r in sorted(self.inv.robots.values(), key=lambda r: r.id):
            if r.provisioned:
                paths += self.render_robot(r)
            else:
                notes.append(f"{r.name}: ещё не прошит (fleet provision --id {r.id})")
        for s in self.inv.sites.values():
            if not s.managed:
                paths += self.render_kit(s)
            elif self.store.has_site(s.name):
                paths += self.render_site(s)
            else:
                notes.append(f"площадка {s.name}: сервер ещё не развёрнут (fleet deploy-server --site {s.name})")
        if self.inv.service:
            if self.store.has_service():
                paths += self.render_service()
            else:
                notes.append("сервисный туннель: сервер ещё не развёрнут (fleet deploy-server --service)")
        return paths, notes
