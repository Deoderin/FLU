"""Inventory: площадки, роботы, операторы и платформы, плюс их проверка.

Человек правит YAML-файлы в inventory/ и base/, fleet их читает и проверяет.
Единственное, что fleet пишет обратно, — site и slot робота (provision / move).
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import zlib
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from . import addressing as A

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
ROBOT_LAN = A.LAN
CGNAT = ipaddress.ip_network("100.64.0.0/10")
ROLES = {"operator", "dispatcher"}
PLATFORM_SECTIONS = ("wan", "ports", "fc", "failsafe", "cameras", "modes")


class InventoryError(Exception):
    """Ошибка в inventory: сообщение показывается человеку как есть."""


def _yaml() -> YAML:
    y = YAML()  # round-trip: комментарии в robots.yaml сохраняются при записи
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
        raise InventoryError(f"{what}: некорректная сеть {value!r} ({exc})") from None
    if net.version != 4:
        raise InventoryError(f"{what}: нужна IPv4-сеть, получено {value}")
    return net


def _ip(value: Any, what: str) -> IPv4Address:
    try:
        return ipaddress.IPv4Address(str(value))
    except ValueError:
        raise InventoryError(f"{what}: некорректный адрес {value!r}") from None


@dataclass
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: Any, what: str) -> Endpoint:
        host, _, port = str(value).rpartition(":")
        if not host or not port.isdigit():
            raise InventoryError(f"{what}: endpoint должен быть host:port, получено {value!r}")
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
    name: str
    kind: str  # managed | external
    customer: str
    endpoint: Endpoint
    network: SiteNetwork
    host: str | None = None  # ssh-хост для deploy-server (managed)
    ssh_user: str = "root"
    vpn: str = "wireguard"
    server_pubkey: str | None = None  # external: ключ сервера заказчика
    server_ip: IPv4Address | None = None  # external: адрес сервера заказчика в tunnel
    allowed_sources: list[IPv4Address] = field(default_factory=list)

    @property
    def managed(self) -> bool:
        return self.kind == "managed"

    @property
    def iface(self) -> str:
        """Имя интерфейса WireGuard площадки — и на сервере, и на MikroTik."""
        return f"wg-{self.name}"

    @property
    def robot_listen_port(self) -> int:
        """Локальный порт WireGuard на MikroTik. Во время переезда у робота
        работают две площадки сразу, поэтому порт у каждой площадки свой."""
        return 20000 + zlib.crc32(self.name.encode()) % 10000

    def sources(self) -> list[IPv4Address]:
        """Кому робот разрешает MAVLink и RTSP."""
        return self.allowed_sources or [A.server_ip(self)]


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
        return f"flu-{self.id:03d}"

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

    # --- загрузка ---------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> Inventory:
        y = _yaml()

        def read(rel: str) -> Any:
            path = root / rel
            if not path.exists():
                raise InventoryError(f"нет файла {rel}")
            return y.load(path.read_text("utf-8")) or {}

        sites_doc = read("inventory/sites.yaml")
        robots_doc = read("inventory/robots.yaml")
        ops_doc = read("inventory/operators.yaml")
        platforms_doc = read("base/platforms.yaml")

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
            what = f"площадка {name}"
            sites[name] = Site(
                name=str(name),
                kind=str(s.get("kind", "managed")),
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
        for r in robots_doc.get("robots") or []:
            robot = Robot(
                id=int(r["id"]),
                platform=str(r["platform"]),
                serial=str(r.get("serial", "")),
                site=r.get("site"),
                slot=int(r["slot"]) if r.get("slot") is not None else None,
                service_tunnel=bool(r.get("service_tunnel", False)),
            )
            if robot.id in robots:
                raise InventoryError(f"робот id {robot.id} описан дважды")
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
                raise InventoryError(f"оператор {op.name} описан дважды")
            operators[op.name] = op

        platforms = {str(k): dict(v) for k, v in (platforms_doc.get("platforms") or {}).items()}
        return cls(root, service, sites, robots, operators, platforms, robots_doc)

    # --- доступ -----------------------------------------------------------

    def robot(self, robot_id: int) -> Robot:
        if robot_id not in self.robots:
            raise InventoryError(f"робота id {robot_id} нет в inventory/robots.yaml")
        return self.robots[robot_id]

    def site(self, name: str) -> Site:
        if name not in self.sites:
            raise InventoryError(f"площадки {name} нет в inventory/sites.yaml")
        return self.sites[name]

    def platform(self, robot: Robot) -> dict:
        return self.platforms[robot.platform]

    def robots_on(self, site_name: str) -> list[Robot]:
        """Прошитые роботы площадки, по возрастанию slot."""
        rs = [r for r in self.robots.values() if r.site == site_name and r.slot is not None]
        return sorted(rs, key=lambda r: r.slot)

    def operators_on(self, site_name: str) -> list[Operator]:
        return sorted((o for o in self.operators.values() if site_name in o.sites), key=lambda o: o.id)

    def free_slot(self, site_name: str, prefer: int | None = None) -> int:
        """Свободный slot на площадке. prefer — попробовать сохранить прежний
        (тогда при переезде не придётся менять SYSID в полётном контроллере)."""
        site = self.site(site_name)
        busy = {r.slot for r in self.robots_on(site_name)}
        cap = A.capacity(site)
        if prefer is not None and 1 <= prefer <= cap and prefer not in busy:
            return prefer
        for slot in range(1, cap + 1):
            if slot not in busy:
                return slot
        raise InventoryError(f"на площадке {site_name} нет свободных мест (вместимость {cap})")

    # --- запись -----------------------------------------------------------

    def assign(self, robot_id: int, site_name: str, slot: int) -> None:
        robot = self.robot(robot_id)
        robot.site, robot.slot = site_name, slot
        for item in self._robots_doc["robots"]:
            if int(item["id"]) != robot_id:
                continue
            item["site"] = site_name
            if "slot" in item:
                item["slot"] = slot
            else:  # сразу после site, чтобы запись читалась
                item.insert(list(item).index("site") + 1, "slot", slot)
            return

    def save_robots(self) -> None:
        path = self.root / "inventory" / "robots.yaml"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            _yaml().dump(self._robots_doc, f)

    # --- проверка ---------------------------------------------------------

    def validate(self) -> list[str]:
        errors: list[str] = []
        e = errors.append

        for name, p in self.platforms.items():
            for section in PLATFORM_SECTIONS:
                if section not in p:
                    e(f"платформа {name}: нет раздела {section}")

        nets: list[tuple[IPv4Network, str]] = []
        if self.service:
            svc = self.service
            nets.append((svc.network, "service.network"))
            if svc.network.prefixlen > 23:
                e("service.network: нужна сеть не меньше /23 (первая /24 — сервер и инженеры)")
            for eng in svc.engineers:
                if not 1 <= eng.id <= 253:
                    e(f"инженер {eng.name}: id должен быть 1..253")

        for s in self.sites.values():
            what = f"площадка {s.name}"
            if not NAME_RE.match(s.name):
                e(f"{what}: имя только из a-z, 0-9 и '-', до 20 символов")
            if s.kind not in ("managed", "external"):
                e(f"{what}: kind должен быть managed или external")
            if s.managed and s.network.operators is None:
                e(f"{what}: у managed-площадки нужна сеть operators")
            if not s.managed and not (s.server_pubkey and is_wg_key(s.server_pubkey)):
                e(f"{what}: нужен server_pubkey — открытый ключ WireGuard сервера заказчика")
            if s.network.robot_lans.prefixlen > A.BLOCK_PREFIX:
                e(f"{what}: robot_lans должна быть не меньше /{A.BLOCK_PREFIX}")
            if A.capacity(s) == 0:
                e(f"{what}: в сетях tunnel / robot_lans не помещается ни один робот")
            if A.server_ip(s) not in s.network.tunnel:
                e(f"{what}: server_ip {A.server_ip(s)} не входит в tunnel {s.network.tunnel}")
            for label, net in s.network.items():
                nets.append((net, f"{s.name}.{label}"))

        for i, (a, an) in enumerate(nets):
            for bad, label in ((ROBOT_LAN, "внутренней сетью робота"), (CGNAT, "CGNAT операторов связи")):
                if a.overlaps(bad):
                    e(f"{an} ({a}) пересекается с {label} ({bad})")
            for b, bn in nets[i + 1:]:
                if a.overlaps(b):
                    e(f"{an} ({a}) пересекается с {bn} ({b})")

        ports: dict[int, str] = {}
        for s in self.sites.values():
            port = s.robot_listen_port
            if port in ports:
                e(f"площадки {ports[port]} и {s.name} получили один порт WireGuard на MikroTik ({port}): переименуйте одну")
            ports[port] = s.name

        taken: dict[tuple[str, int], int] = {}
        for r in self.robots.values():
            what = f"робот {r.id}"
            if r.platform not in self.platforms:
                e(f"{what}: нет платформы {r.platform} в base/platforms.yaml")
            site = self.sites.get(r.site) if r.site else None
            if r.site and site is None:
                e(f"{what}: нет площадки {r.site}")
            if r.slot is not None:
                if site is None:
                    e(f"{what}: slot задан без площадки")
                elif not 1 <= r.slot <= A.capacity(site):
                    e(f"{what}: slot {r.slot} вне вместимости площадки {site.name} (1..{A.capacity(site)})")
                elif (site.name, r.slot) in taken:
                    e(f"{what}: slot {r.slot} на {site.name} уже занят роботом {taken[(site.name, r.slot)]}")
                else:
                    taken[(site.name, r.slot)] = r.id
                    if A.robot_tunnel_ip(site, r.slot) == A.server_ip(site):
                        e(f"{what}: адрес в туннеле совпадает с адресом сервера площадки")
            if r.service_tunnel:
                if self.service is None:
                    e(f"{what}: service_tunnel включён, но раздел service в sites.yaml не задан")
                elif A.service_robot_ip(self.service, r.id) >= self.service.network.broadcast_address:
                    e(f"{what}: id не помещается в service.network")

        op_ids: dict[tuple[str, int], str] = {}
        for o in self.operators.values():
            what = f"оператор {o.name}"
            if o.role not in ROLES:
                e(f"{what}: role должна быть operator или dispatcher")
            for sn in o.sites:
                site = self.sites.get(sn)
                if site is None:
                    e(f"{what}: нет площадки {sn}")
                    continue
                if not site.managed:
                    e(f"{what}: операторы бывают только у managed-площадок, а {sn} — external")
                    continue
                if not 1 <= o.id < site.network.operators.num_addresses - 1:
                    e(f"{what}: id {o.id} не помещается в сеть operators площадки {sn}")
                if (sn, o.id) in op_ids:
                    e(f"{what}: id {o.id} на {sn} уже занят оператором {op_ids[(sn, o.id)]}")
                op_ids[(sn, o.id)] = o.name

        return errors

    def check(self) -> None:
        errors = self.validate()
        if errors:
            raise InventoryError("ошибки в inventory:\n  - " + "\n  - ".join(errors))
