"""Inventory: майданчики, НРК, оператори та платформи, а також їхня перевірка.

Людина править YAML-файли в inventory/ і base/, fleet їх читає та перевіряє.
Єдине, що fleet записує назад, — site і slot НРК (provision / move).
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import shutil
import zlib
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from . import addressing as A
from .i18n import T

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
ROBOT_LAN = A.LAN
CGNAT = ipaddress.ip_network("100.64.0.0/10")
ROLES = {"operator", "dispatcher"}
PLATFORM_SECTIONS = ("wan", "ports", "fc", "failsafe", "cameras", "modes")
# У git лежать лише порожні шаблони. Справжній inventory/ (сервер, пілоти, НРК) — тільки на цьому
# ноутбуці (.gitignore): разом із кодом він не має потрапити до інших.
TEMPLATE_DIR = "inventory.example"


class InventoryError(Exception):
    """Помилка в inventory: повідомлення показується людині як є."""


def ensure_inventory(root: Path) -> None:
    """Перший запуск на новому ноутбуці: inventory/ створюється з шаблонів. Наявні файли не чіпаємо."""
    src, dst = root / TEMPLATE_DIR, root / "inventory"
    if not src.is_dir():
        return
    dst.mkdir(exist_ok=True)
    for template in sorted(src.glob("*.yaml")):
        if not (dst / template.name).exists():
            shutil.copyfile(template, dst / template.name)


def _yaml() -> YAML:
    y = YAML()  # round-trip: коментарі в robots.yaml зберігаються під час запису
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def is_wg_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def _net(value: Any, what: str) -> IPv4Network:
    try:
        net = ipaddress.ip_network(str(value), strict=True)
    except ValueError as exc:
        raise InventoryError(T(f"{what}: некоректна мережа {value!r} ({exc})",
                               f"{what}: invalid network {value!r} ({exc})")) from None
    if net.version != 4:
        raise InventoryError(T(f"{what}: потрібна мережа IPv4, отримано {value}",
                               f"{what}: an IPv4 network is required, got {value}"))
    return net


def _ip(value: Any, what: str) -> IPv4Address:
    try:
        return ipaddress.IPv4Address(str(value))
    except ValueError:
        raise InventoryError(T(f"{what}: некоректна адреса {value!r}", f"{what}: invalid address {value!r}")) from None


@dataclass
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: Any, what: str) -> Endpoint:
        host, _, port = str(value).rpartition(":")
        if not host or not port.isdigit():
            raise InventoryError(T(f"{what}: endpoint має бути host:port, отримано {value!r}",
                                   f"{what}: endpoint must be host:port, got {value!r}"))
        return cls(host, int(port))

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class SiteNetwork:
    tunnel: IPv4Network
    robot_lans: IPv4Network
    operators: IPv4Network | None = None

    def items(self) -> list[tuple[str, IPv4Network]]:
        nets = [("tunnel", self.tunnel), ("robot_lans", self.robot_lans)]
        if self.operators is not None:
            nets.append(("operators", self.operators))
        return nets


@dataclass
class Site:
    """kind:
    hub      — наш сервер, налаштований командою, яку пілот вставляє на сервері (лише WireGuard):
               сервер пересилає трафік пілотів до НРК, ключ сервера живе лише на сервері;
    managed  — наш стек (WireGuard + MediaMTX + арбітр), розгортається fleet deploy-server;
    external — сервер замовника, ми видаємо лише файли підключення.
    """
    name: str
    kind: str  # hub | managed | external
    customer: str
    endpoint: Endpoint
    network: SiteNetwork
    host: str | None = None  # ssh-хост для deploy-server (managed)
    ssh_user: str = "root"
    vpn: str = "wireguard"
    server_pubkey: str | None = None  # external: ключ сервера замовника
    server_ip: IPv4Address | None = None  # external: адреса сервера замовника в tunnel
    allowed_sources: list[IPv4Address] = field(default_factory=list)

    @property
    def managed(self) -> bool:
        return self.kind == "managed"

    @property
    def hub(self) -> bool:
        return self.kind == "hub"

    @property
    def has_pilots(self) -> bool:
        return self.kind in ("managed", "hub")

    @property
    def iface(self) -> str:
        """Ім'я інтерфейсу WireGuard майданчика — і на сервері, і на MikroTik."""
        return f"wg-{self.name}"

    @property
    def robot_listen_port(self) -> int:
        """Локальний порт WireGuard на MikroTik. Під час переїзду в НРК
        працюють два майданчики одночасно, тому порт у кожного свій."""
        return 20000 + zlib.crc32(self.name.encode()) % 10000

    def sources(self) -> list[str]:
        """Кому НРК дозволяє MAVLink і RTSP. На hub пілоти ходять до НРК напряму через сервер."""
        if self.hub:
            return [str(A.server_ip(self)), str(self.network.operators)]
        return [str(ip) for ip in self.allowed_sources] or [str(A.server_ip(self))]

    def mgmt_sources(self) -> list[str]:
        """Кому НРК дозволяє SSH до MikroTik через тунель майданчика."""
        if self.managed:
            return [str(A.server_ip(self))]
        if self.hub:
            return [str(self.network.operators)]
        return []

    def peer_allowed(self) -> list[str]:
        """allowed-address пира сервера на MikroTik: звідки НРК приймає трафік у тунелі."""
        nets = [str(self.network.tunnel)]
        if self.hub:
            nets.append(str(self.network.operators))  # пілоти приходять зі своїх адрес
        return nets

    def robot_routes(self) -> list[str]:
        """Маршрути на MikroTik через тунель: відповіді пілотам мають іти в тунель, а не в LTE."""
        return [str(self.network.operators)] if self.hub else []


@dataclass
class Engineer:
    name: str
    id: int


@dataclass
class Service:
    endpoint: Endpoint
    network: IPv4Network
    host: str | None = None
    ssh_user: str = "root"
    engineers: list[Engineer] = field(default_factory=list)

    iface = "wg-svc"


@dataclass
class Robot:
    id: int
    platform: str
    serial: str = ""
    site: str | None = None
    slot: int | None = None
    service_tunnel: bool = False

    @property
    def name(self) -> str:
        """Технічне ім'я НРК: ідентифікатор MikroTik, назви файлів і потоків. Лише ASCII."""
        return f"nrk-{self.id:03d}"

    @property
    def provisioned(self) -> bool:
        return self.site is not None and self.slot is not None


@dataclass
class Operator:
    name: str
    id: int
    role: str = "operator"
    sites: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    root: Path
    service: Service | None
    sites: dict[str, Site]
    robots: dict[int, Robot]
    operators: dict[str, Operator]
    platforms: dict[str, dict]
    _robots_doc: Any = field(default=None, repr=False)

    # --- завантаження -----------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> Inventory:
        ensure_inventory(root)
        y = _yaml()

        def read(rel: str) -> Any:
            path = root / rel
            if not path.exists():
                raise InventoryError(T(f"немає файлу {rel}", f"file {rel} not found"))
            return y.load(path.read_text("utf-8")) or {}

        sites_doc = read("inventory/sites.yaml")
        robots_doc = read("inventory/robots.yaml")
        ops_doc = read("inventory/operators.yaml")
        platforms_doc = read("base/platforms.yaml")
        if robots_doc.get("robots") is None:
            robots_doc["robots"] = []

        service = None
        if sites_doc.get("service"):
            s = sites_doc["service"]
            service = Service(
                endpoint=Endpoint.parse(s["endpoint"], "service.endpoint"),
                network=_net(s["network"], "service.network"),
                host=s.get("host"),
                ssh_user=s.get("ssh_user", "root"),
                engineers=[Engineer(str(e["name"]), int(e["id"])) for e in s.get("engineers") or []],
            )

        sites = {}
        for name, s in (sites_doc.get("sites") or {}).items():
            net = s.get("network") or {}
            what = T(f"майданчик {name}", f"site {name}")
            sites[name] = Site(
                name=str(name),
                kind=str(s.get("kind", "hub")),
                customer=str(s.get("customer", "")),
                endpoint=Endpoint.parse(s.get("endpoint"), what),
                network=SiteNetwork(
                    tunnel=_net(net.get("tunnel"), f"{what}: tunnel"),
                    robot_lans=_net(net.get("robot_lans"), f"{what}: robot_lans"),
                    operators=_net(net["operators"], f"{what}: operators") if net.get("operators") else None,
                ),
                host=s.get("host"),
                ssh_user=s.get("ssh_user", "root"),
                vpn=str(s.get("vpn", "wireguard")),
                server_pubkey=s.get("server_pubkey"),
                server_ip=_ip(s["server_ip"], f"{what}: server_ip") if s.get("server_ip") else None,
                allowed_sources=[_ip(a, f"{what}: allowed_sources") for a in s.get("allowed_sources") or []],
            )

        robots = {}
        for r in robots_doc["robots"]:
            robot = Robot(
                id=int(r["id"]),
                platform=str(r["platform"]),
                serial=str(r.get("serial", "")),
                site=r.get("site"),
                slot=int(r["slot"]) if r.get("slot") is not None else None,
                service_tunnel=bool(r.get("service_tunnel", False)),
            )
            if robot.id in robots:
                raise InventoryError(T(f"НРК з id {robot.id} описано двічі", f"UGV id {robot.id} is listed twice"))
            robots[robot.id] = robot

        operators = {}
        for o in ops_doc.get("operators") or []:
            op = Operator(
                name=str(o["name"]),
                id=int(o["id"]),
                role=str(o.get("role", "operator")),
                sites=[str(x) for x in o.get("sites") or []],
            )
            if op.name in operators:
                raise InventoryError(T(f"оператора {op.name} описано двічі", f"operator {op.name} is listed twice"))
            operators[op.name] = op

        platforms = {str(k): dict(v) for k, v in (platforms_doc.get("platforms") or {}).items()}
        return cls(root, service, sites, robots, operators, platforms, robots_doc)

    # --- доступ -----------------------------------------------------------

    def robot(self, robot_id: int) -> Robot:
        if robot_id not in self.robots:
            raise InventoryError(T(f"НРК з id {robot_id} немає в inventory/robots.yaml",
                                   f"UGV id {robot_id} is not in inventory/robots.yaml"))
        return self.robots[robot_id]

    def site(self, name: str) -> Site:
        if name not in self.sites:
            raise InventoryError(T(f"майданчика {name} немає в inventory/sites.yaml",
                                   f"site {name} is not in inventory/sites.yaml"))
        return self.sites[name]

    def platform(self, robot: Robot) -> dict:
        return self.platforms[robot.platform]

    def robots_on(self, site_name: str) -> list[Robot]:
        """Зареєстровані НРК майданчика, за зростанням slot."""
        rs = [r for r in self.robots.values() if r.site == site_name and r.slot is not None]
        return sorted(rs, key=lambda r: r.slot)

    def operators_on(self, site_name: str) -> list[Operator]:
        return sorted((o for o in self.operators.values() if site_name in o.sites), key=lambda o: o.id)

    def free_slot(self, site_name: str, prefer: int | None = None) -> int:
        """Вільний slot на майданчику. prefer — спробувати зберегти попередній
        (тоді під час переїзду не доведеться міняти SYSID у польотному контролері)."""
        site = self.site(site_name)
        busy = {r.slot for r in self.robots_on(site_name)}
        cap = A.capacity(site)
        if prefer is not None and 1 <= prefer <= cap and prefer not in busy:
            return prefer
        for slot in range(1, cap + 1):
            if slot not in busy:
                return slot
        raise InventoryError(T(f"на майданчику {site_name} немає вільних місць (місткість {cap})",
                               f"site {site_name} is full (capacity {cap})"))

    # --- запис ------------------------------------------------------------

    def assign(self, robot_id: int, site_name: str, slot: int) -> None:
        robot = self.robot(robot_id)
        robot.site, robot.slot = site_name, slot
        for item in self._robots_doc["robots"]:
            if int(item["id"]) != robot_id:
                continue
            item["site"] = site_name
            if "slot" in item:
                item["slot"] = slot
            else:  # одразу після site, щоб запис читався
                item.insert(list(item).index("site") + 1, "slot", slot)
            return

    def save_robots(self) -> None:
        path = self.root / "inventory" / "robots.yaml"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            _yaml().dump(self._robots_doc, f)

    # --- перевірка --------------------------------------------------------

    def validate(self) -> list[str]:
        errors: list[str] = []
        e = errors.append

        for name, p in self.platforms.items():
            for section in PLATFORM_SECTIONS:
                if section not in p:
                    e(T(f"платформа {name}: немає розділу {section}", f"platform {name}: section {section} is missing"))

        nets: list[tuple[IPv4Network, str]] = []
        if self.service:
            svc = self.service
            nets.append((svc.network, "service.network"))
            if svc.network.prefixlen > 23:
                e(T("service.network: потрібна мережа не менша за /23 (перша /24 — сервер та інженери)",
                    "service.network: needs at least a /23 (the first /24 is the server and engineers)"))
            for eng in svc.engineers:
                if not 1 <= eng.id <= 253:
                    e(T(f"інженер {eng.name}: id має бути 1..253", f"engineer {eng.name}: id must be 1..253"))

        for s in self.sites.values():
            what = T(f"майданчик {s.name}", f"site {s.name}")
            if not NAME_RE.match(s.name):
                e(T(f"{what}: ім'я лише з a-z, 0-9 і '-', до 20 символів",
                    f"{what}: the name may contain only a-z, 0-9 and '-', up to 20 characters"))
            if s.kind not in ("hub", "managed", "external"):
                e(T(f"{what}: kind має бути hub, managed або external", f"{what}: kind must be hub, managed or external"))
            if s.has_pilots and s.network.operators is None:
                e(T(f"{what}: майданчику з пілотами потрібна мережа operators", f"{what}: a site with pilots needs an operators network"))
            # hub отримує ключ сервера з відповіді серверного скрипту: до першого НРК його ще немає
            need_key = s.kind == "external" or (s.hub and any(r.site == s.name and r.slot for r in self.robots.values()))
            if s.server_pubkey and not is_wg_key(s.server_pubkey):
                e(T(f"{what}: server_pubkey — некоректний ключ WireGuard", f"{what}: server_pubkey is not a valid WireGuard key"))
            elif need_key and not s.server_pubkey:
                e(T(f"{what}: потрібен server_pubkey — відкритий ключ WireGuard сервера",
                    f"{what}: server_pubkey is required - the WireGuard public key of the server"))
            if s.network.robot_lans.prefixlen > A.BLOCK_PREFIX:
                e(T(f"{what}: robot_lans має бути не менша за /{A.BLOCK_PREFIX}",
                    f"{what}: robot_lans must be at least /{A.BLOCK_PREFIX}"))
            if A.capacity(s) == 0:
                e(T(f"{what}: у мережах tunnel / robot_lans не вміщується жоден НРК",
                    f"{what}: the tunnel / robot_lans networks cannot fit a single UGV"))
            if A.server_ip(s) not in s.network.tunnel:
                e(T(f"{what}: server_ip {A.server_ip(s)} не входить у tunnel {s.network.tunnel}",
                    f"{what}: server_ip {A.server_ip(s)} is outside tunnel {s.network.tunnel}"))
            for label, net in s.network.items():
                nets.append((net, f"{s.name}.{label}"))

        bad_nets = (
            (ROBOT_LAN, T("внутрішньою мережею НРК", "the UGV internal network")),
            (CGNAT, T("CGNAT операторів зв'язку", "carrier CGNAT")),
        )
        for i, (a, an) in enumerate(nets):
            for bad, label in bad_nets:
                if a.overlaps(bad):
                    e(T(f"{an} ({a}) перетинається з {label} ({bad})", f"{an} ({a}) overlaps {label} ({bad})"))
            for b, bn in nets[i + 1:]:
                if a.overlaps(b):
                    e(T(f"{an} ({a}) перетинається з {bn} ({b})", f"{an} ({a}) overlaps {bn} ({b})"))

        ports: dict[int, str] = {}
        for s in self.sites.values():
            port = s.robot_listen_port
            if port in ports:
                e(T(f"майданчики {ports[port]} і {s.name} отримали однаковий порт WireGuard на MikroTik ({port}): перейменуйте один",
                    f"sites {ports[port]} and {s.name} got the same WireGuard port on MikroTik ({port}): rename one"))
            ports[port] = s.name

        taken: dict[tuple[str, int], int] = {}
        for r in self.robots.values():
            what = T(f"НРК {r.id}", f"UGV {r.id}")
            if r.platform not in self.platforms:
                e(T(f"{what}: немає платформи {r.platform} у base/platforms.yaml",
                    f"{what}: platform {r.platform} is not in base/platforms.yaml"))
            site = self.sites.get(r.site) if r.site else None
            if r.site and site is None:
                e(T(f"{what}: немає майданчика {r.site}", f"{what}: site {r.site} does not exist"))
            if r.slot is not None:
                if site is None:
                    e(T(f"{what}: slot задано без майданчика", f"{what}: slot is set without a site"))
                elif not 1 <= r.slot <= A.capacity(site):
                    e(T(f"{what}: slot {r.slot} поза місткістю майданчика {site.name} (1..{A.capacity(site)})",
                        f"{what}: slot {r.slot} is outside the capacity of site {site.name} (1..{A.capacity(site)})"))
                elif (site.name, r.slot) in taken:
                    other = taken[(site.name, r.slot)]
                    e(T(f"{what}: slot {r.slot} на {site.name} вже зайнятий НРК {other}",
                        f"{what}: slot {r.slot} on {site.name} is already taken by UGV {other}"))
                else:
                    taken[(site.name, r.slot)] = r.id
                    if A.robot_tunnel_ip(site, r.slot) == A.server_ip(site):
                        e(T(f"{what}: адреса в тунелі збігається з адресою сервера майданчика",
                            f"{what}: the tunnel address equals the site server address"))
            if r.service_tunnel:
                if self.service is None:
                    e(T(f"{what}: service_tunnel увімкнено, але розділ service у sites.yaml не задано",
                        f"{what}: service_tunnel is on but the service section is missing in sites.yaml"))
                elif A.service_robot_ip(self.service, r.id) >= self.service.network.broadcast_address:
                    e(T(f"{what}: id не вміщується в service.network", f"{what}: id does not fit into service.network"))

        op_ids: dict[tuple[str, int], str] = {}
        for o in self.operators.values():
            what = T(f"оператор {o.name}", f"operator {o.name}")
            if o.role not in ROLES:
                e(T(f"{what}: role має бути operator або dispatcher", f"{what}: role must be operator or dispatcher"))
            for sn in o.sites:
                site = self.sites.get(sn)
                if site is None:
                    e(T(f"{what}: немає майданчика {sn}", f"{what}: site {sn} does not exist"))
                    continue
                if not site.has_pilots:
                    e(T(f"{what}: пілоти бувають лише на hub і managed-майданчиках, а {sn} — external",
                        f"{what}: only hub and managed sites have pilots, and {sn} is external"))
                    continue
                if not 1 <= o.id < site.network.operators.num_addresses - 1:
                    e(T(f"{what}: id {o.id} не вміщується в мережу operators майданчика {sn}",
                        f"{what}: id {o.id} does not fit into the operators network of site {sn}"))
                if (sn, o.id) in op_ids:
                    e(T(f"{what}: id {o.id} на {sn} вже зайнятий оператором {op_ids[(sn, o.id)]}",
                        f"{what}: id {o.id} on {sn} is already taken by operator {op_ids[(sn, o.id)]}"))
                op_ids[(sn, o.id)] = o.name

        return errors

    def check(self) -> None:
        errors = self.validate()
        if errors:
            raise InventoryError(T("помилки в inventory:", "inventory errors:") + "\n  - " + "\n  - ".join(errors))
