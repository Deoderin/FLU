"""Збирання конфігів з inventory, секретів і шаблонів у build/.

build/robots/<нрк>/           robot.rsc, robot.parm, expected.parm, move-*.rsc
build/sites/<майданчик>/server/    бандл стеку майданчика для deploy-server
build/sites/<майданчик>/operators/ конфіги WireGuard операторів і пам'ятка з адресами
build/sites/<майданчик>/kit/       файли підключення для сервера замовника (external)
build/service/                сервісний тунель: сервер і конфіги інженерів
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
from .i18n import T, get_lang
from .inventory import Inventory, Robot, Site
from .keystore import SecretError, SecretStore, wg_pubkey
from .params import expected_params, format_parm, load_ignore, parse_parm, read_parm

MTU = 1280  # мінімум IPv6: проходить і через LTE, і через Starlink без фрагментації, яка псує відео
MEDIAMTX_VERSION = "1.21.1"
SVC_LISTEN_PORT = 13230  # порт WireGuard сервісного тунелю на MikroTik
ARBITER_FILES = ("Dockerfile", "requirements.txt", "arbiter.py", "policy.py", "panel.html")
FP_PLACEHOLDER = "@FINGERPRINT@"


class RenderError(Exception):
    pass


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:  # LF: для bash і RouterOS
        f.write(text)
    return path


def ensure_ascii(text: str, what: str) -> None:
    """RouterOS погано переносить UTF-8 у конфігу: ловимо це під час збирання, а не на НРК."""
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.isascii():
            raise RenderError(T(f"{what}, рядок {lineno}: у конфігу MikroTik допустимий лише ASCII: {line!r}",
                                f"{what}, line {lineno}: only ASCII is allowed in a MikroTik config: {line!r}"))


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
        if site.managed:
            return wg_pubkey(self.store.site(site.name)["wg"])
        if not site.server_pubkey:
            raise RenderError(T(f"ключ сервера майданчика {site.name} ще невідомий: вставте відповідь сервера",
                                f"the server key of site {site.name} is not known yet: paste the server response"))
        return site.server_pubkey

    # --- НРК -----------------------------------------------------------------

    @staticmethod
    def lan_ctx(robot: Robot) -> dict:
        """Мережа НРК: роутер і DHCP — 192.168.88.x, пристрої — на адресах, знайдених під час прошивки."""
        return {"net": str(A.LAN), "mapped": str(A.LAN_MAPPED), "router": str(A.lan_ip("router")),
                **{h: str(A.device_ip(robot, h)) for h in A.DEVICES}, "extra": A.device_nets(robot),
                # Закріплені адреси: пристрій, що бере адресу по DHCP, після перезавантаження отримає ту саму
                "leases": [{"host": h, **d} for h, d in robot.devices.items() if d.get("mac")]}

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
            "allowed": ",".join(site.peer_allowed()),
            "routes": site.robot_routes(),
            "robot_ip": str(A.robot_tunnel_ip(site, slot)),
            "prefix": site.network.tunnel.prefixlen,
            "lan_block": str(A.lan_block(site, slot)),
            "lan_mapped": str(A.LAN_MAPPED),
            "dstnat": [{"outside": str(A.mapped_ip(site, slot, h)), "inside": str(A.device_ip(robot, h))}
                       for h in A.device_dstnat(robot)],
            "sources": site.sources(),
            "mgmt": site.mgmt_sources(),
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
        """Повний конфіг MikroTik і його відбиток (за ним тест ловить застарілий конфіг)."""
        plat = self.inv.platform(robot)
        ctx = {
            "robot": robot,
            "platform": plat,
            "slot": slot,
            "lan": self.lan_ctx(robot),
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
            lan=self.lan_ctx(robot),
        )
        return text, parse_parm(text)

    def expected_params(self, robot: Robot) -> dict[str, float]:
        """Еталон параметрів НРК: база платформи без калібрувань + параметри fleet."""
        base_name = self.inv.platform(robot)["fc"].get("base_params", "ardupilot-base.parm")
        base = read_parm(self.inv.root / "base" / base_name)
        patterns = load_ignore(self.inv.root / "base" / "ardupilot-ignore.txt")
        _, overrides = self.robot_params(robot, robot.site, robot.slot)
        return expected_params(base, overrides, patterns)

    def render_robot(self, robot: Robot) -> list[Path]:
        if not robot.provisioned:
            raise RenderError(T(f"{robot.name} ще не зареєстровано: fleet provision --id {robot.id}",
                                f"{robot.name} is not registered yet: fleet provision --id {robot.id}"))
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
        """Скрипти переїзду: додати новий майданчик (з автовідкатом) і підтвердити переїзд."""
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

    # --- майданчик -----------------------------------------------------------

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
                "camera_password": plat["cameras"].get("rtsp_password") or sec["camera_password"],
                "hold_mode": plat["modes"]["hold"],
            })
        return rows

    def render_site(self, site: Site) -> list[Path]:
        if not site.managed:
            raise RenderError(T(f"майданчик {site.name} — external: для нього fleet kit",
                                f"site {site.name} is external: use fleet kit"))
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
            "server/install.sh.j2", title=f"site {site.name}", iface=site.iface, mtu=MTU, with_stack=True)))

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
            T(f"# Майданчик {site.name}: підключення операторів", f"# Site {site.name}: operator access"),
            "",
            T("1. Імпортуйте свій конфіг із цієї теки у WireGuard і ввімкніть тунель.",
              "1. Import your config from this folder into WireGuard and enable the tunnel."),
            T(f"2. Панель керування (хто володіє НРК, передача): http://{sip}:{A.PANEL_PORT}",
              f"2. Control panel (who owns which UGV, handover): http://{sip}:{A.PANEL_PORT}"),
            T("3. У QGroundControl / Mission Planner додайте TCP-підключення до потрібного НРК.",
              "3. In QGroundControl / Mission Planner add a TCP link to the UGV you need."),
            T("   MAVLink system ID наземної станції лишіть 255 (за замовчуванням).",
              "   Keep the ground station MAVLink system ID at 255 (the default)."),
            "",
            T("| НРК | MAVLink (TCP) | Передня камера | Задня камера |", "| UGV | MAVLink (TCP) | Front camera | Rear camera |"),
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
        """Файли підключення для адміністратора сервера замовника (external)."""
        robots = self._robot_rows(site)
        out = self.site_dir(site) / "kit"
        failsafe = {self.inv.platform(r["robot"])["failsafe"]["gcs_timeout_s"] for r in robots} or {5}
        readme = self.tpl(
            f"integration/README.{get_lang()}.md.j2",
            site=site,
            server_ip=str(A.server_ip(site)),
            robots=robots,
            mtu=MTU,
            failsafe_s=max(failsafe),
        )
        peers = [f"# fleet: UGVs of site {site.name} - add to the WireGuard server config"]
        for r in robots:
            peers += ["", f"# UGV {r['name']} (id {r['id']})", "[Peer]",
                      f"PublicKey = {r['public_key']}", f"AllowedIPs = {r['tunnel_ip']}/32, {r['lan_block']}"]
        creds = io.StringIO()
        w = csv.writer(creds, lineterminator="\n")
        w.writerow(["ugv", "camera", "rtsp_url", "user", "password"])
        for r in robots:
            for cam in A.CAMERAS:
                w.writerow([r["name"], cam, f"rtsp://{r[cam + '_ip']}:554{r['rtsp_path']}", r["rtsp_user"], r["camera_password"]])
        return [
            write(out / "README.md", readme),
            write(out / "peers.conf", "\n".join(peers) + "\n"),
            write(out / "credentials.csv", creds.getvalue()),
        ]

    def render_pilots(self, site: Site) -> list[Path]:
        """hub: конфіги WireGuard пілотів. Пілот бачить сервер і мережі НРК (сервер пересилає трафік)."""
        if not site.server_pubkey:
            return []  # відповіді сервера ще не було
        sip = A.server_ip(site)
        out = self.site_dir(site) / "operators"
        paths = []
        for o in self.inv.operators_on(site.name):
            paths.append(write(out / f"{o.name}.conf", self.tpl(
                "server/client.conf.j2",
                title=f"pilot {o.name}, site {site.name}",
                private_key=self.store.operator(o.name)["wg"],
                address=str(A.operator_ip(site, o)),
                mtu=MTU,
                server_pubkey=site.server_pubkey,
                endpoint=str(site.endpoint),
                allowed=f"{sip}/32, {site.network.robot_lans}",
            )))
        return paths

    def qgc_address(self, robot: Robot) -> str | None:
        """Куди підключати QGroundControl: hub — напряму до ФК НРК, managed — через арбітр."""
        if not robot.provisioned:
            return None
        site = self.inv.site(robot.site)
        if site.hub:
            return f"{A.mapped_ip(site, robot.slot, 'fc')}:{self.inv.platform(robot)['fc']['mavlink_port']}"
        if site.managed:
            return f"{A.server_ip(site)}:{A.arbiter_port(robot.slot)}"
        return None

    def camera_streams(self, robot: Robot) -> list[dict]:
        """Відео для QGroundControl: hub — напряму з камер через тунель, managed — через MediaMTX сервера."""
        if not robot.provisioned:
            return []
        site = self.inv.site(robot.site)
        cfg = self.inv.platform(robot)["cameras"]
        # Пароль камер однаковий на всіх НРК (камери не перепрошуються): QGroundControl бере його з посилання
        auth = f"{cfg.get('rtsp_user', 'admin')}:{cfg['rtsp_password']}@" if cfg.get("rtsp_password") else ""
        main, second = cfg["rtsp_path"], cfg.get("rtsp_path_secondary") or cfg["rtsp_path"]
        streams = []
        for cam in A.CAMERAS:
            if site.hub:
                ip = str(A.mapped_ip(site, robot.slot, cam))
                base = f"rtsp://{auth}{ip}:554"
                streams.append({"cam": cam, "ip": ip, "rtsp": base + main, "rtsp2": base + second})
            elif site.managed:
                ip = str(A.server_ip(site))
                url = f"rtsp://{ip}:{A.RTSP_PORT}/{robot.name}-{cam}"
                streams.append({"cam": cam, "ip": ip, "rtsp": url, "rtsp2": url})
        return streams

    # --- сервісний тунель ----------------------------------------------------

    def render_service(self) -> list[Path]:
        svc = self.inv.service
        if svc is None:
            raise RenderError(T("розділ service в inventory/sites.yaml не задано",
                                "the service section is missing in inventory/sites.yaml"))
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
                continue  # ще не зареєстровано
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
            "server/install.sh.j2", title="service tunnel hub", iface=svc.iface, mtu=MTU, with_stack=False)))
        return paths

    # --- усе разом -----------------------------------------------------------

    def render_all(self) -> tuple[list[Path], list[str]]:
        paths: list[Path] = []
        notes: list[str] = []
        for r in sorted(self.inv.robots.values(), key=lambda r: r.id):
            if r.provisioned:
                paths += self.render_robot(r)
            else:
                notes.append(T(f"{r.name}: ще не зареєстровано (fleet provision --id {r.id})",
                               f"{r.name}: not registered yet (fleet provision --id {r.id})"))
        for s in self.inv.sites.values():
            if s.hub:
                paths += self.render_pilots(s)
            elif not s.managed:
                paths += self.render_kit(s)
            elif self.store.has_site(s.name):
                paths += self.render_site(s)
            else:
                notes.append(T(f"майданчик {s.name}: сервер ще не розгорнуто (fleet deploy-server --site {s.name})",
                               f"site {s.name}: server not deployed yet (fleet deploy-server --site {s.name})"))
        if self.inv.service:
            if self.store.has_service():
                paths += self.render_service()
            else:
                notes.append(T("сервісний тунель: сервер ще не розгорнуто (fleet deploy-server --service)",
                               "service tunnel: server not deployed yet (fleet deploy-server --service)"))
        return paths, notes
