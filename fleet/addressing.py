"""Адресний план: усі адреси НРК виводяться з майданчика та його slot.

Усередині НРК мережа завжди однакова (192.168.88.0/24). Ззовні, через тунель
майданчика, НРК видно блоком /28 з robot_lans: адреса пристрою = початок блоку + MAPPED[пристрій].
Роутер і ФК потрапляють у блок через netmap (блок -> 192.168.88.0/28), а камери стоять
на .108/.109, за межами /28, тож MikroTik веде до них окремим dst-nat.
"""
from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address, IPv4Network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inventory import Engineer, Operator, Robot, Service, Site

LAN = ipaddress.ip_network("192.168.88.0/24")
LAN_MAPPED = ipaddress.ip_network("192.168.88.0/28")
HOSTS = {"router": 1, "fc": 2, "cam1": 108, "cam2": 109}  # адреса в мережі НРК: 192.168.88.<номер>
MAPPED = {"router": 1, "fc": 2, "cam1": 8, "cam2": 9}     # номер у блоці /28 майданчика
CAMERAS = ("cam1", "cam2")  # cam1 — передня, cam2 — задня
BLOCK_PREFIX = LAN_MAPPED.prefixlen
BLOCK_SIZE = LAN_MAPPED.num_addresses
MAX_SLOT = 250  # slot — це MAVLink SYSID; 255 зайнятий наземною станцією

ARBITER_BASE_PORT = 5760  # оператор підключається до НРК зі slot N на порт 5760 + N
PANEL_PORT = 8080
RTSP_PORT = 8554
WEBRTC_PORT = 8889


def lan_ip(host: str) -> IPv4Address:
    return LAN.network_address + HOSTS[host]


def server_ip(site: Site) -> IPv4Address:
    return site.server_ip or site.network.tunnel.network_address + 1


def robot_tunnel_ip(site: Site, slot: int) -> IPv4Address:
    return site.network.tunnel.network_address + 1 + slot


def lan_block(site: Site, slot: int) -> IPv4Network:
    base = int(site.network.robot_lans.network_address) + (slot - 1) * BLOCK_SIZE
    return ipaddress.ip_network((base, BLOCK_PREFIX))


def mapped_ip(site: Site, slot: int, host: str) -> IPv4Address:
    return lan_block(site, slot).network_address + MAPPED[host]


def dstnat_hosts() -> list[str]:
    """Пристрої поза 192.168.88.0/28: netmap до них не веде, потрібен окремий dst-nat."""
    return [h for h in HOSTS if lan_ip(h) not in LAN_MAPPED]


DEVICES = ("fc", "cam1", "cam2")  # пристрої в портах MikroTik; адреси знаходить прошивка

# Мережа, яку роутер Starlink (сам він — 192.168.1.1) роздає в порт 1. Пристрою в ній роутер НРК не підключає:
# з адресою цієї мережі ще й на мосту він губить шлюз Starlink — НРК без інтернету, а отже й без тунелю, хоч
# прошивка й «вдалася». У ній же заводські адреси камер Dahua (192.168.1.108) і адаптера ФК (192.168.1.200)
STARLINK_NET = ipaddress.ip_network("192.168.1.0/24")
STARLINK_GW = STARLINK_NET.network_address + 1


def in_starlink_net(ip: str | IPv4Address) -> bool:
    return ipaddress.ip_address(ip) in STARLINK_NET


def random_mac(mac: str) -> bool:
    """Локально призначена MAC (біт 0x02 першого байта): випадкові адреси телефонів і ноутбуків у Wi-Fi.
    У камер і адаптерів ФК MAC заводська — пристрій із такою MAC у порту не камера й не ФК."""
    return bool(int(mac[:2], 16) & 0x02)


def untrusted_device(dev: dict) -> str | None:
    """Чому запису про пристрій (з минулої прошивки чи щойно знайденого) не можна вірити; None — можна."""
    if dev.get("mac") and random_mac(dev["mac"]):
        return "random_mac"
    if dev.get("ip") and in_starlink_net(dev["ip"]):
        return "starlink_net"
    return None


def device_ip(robot: Robot, host: str) -> IPv4Address:
    """Адреса пристрою на борту: знайдена під час прошивки (robot.devices) або стандартна 192.168.88.x.
    Записові, якому не можна вірити (untrusted_device), — стандартна: так його не підхопить жоден конфіг."""
    dev = robot.devices.get(host, {})
    found = dev.get("ip") if not untrusted_device(dev) else None
    return ipaddress.ip_address(found) if found else lan_ip(host)


def device_dstnat(robot: Robot) -> list[str]:
    """Пристрої, до яких netmap блоку /28 не веде: стандартні камери (.108/.109) і все, що стоїть не на своїй адресі."""
    return [h for h in DEVICES if device_ip(robot, h) != lan_ip(h) or lan_ip(h) not in LAN_MAPPED]


def device_nets(robot: Robot) -> list[dict]:
    """Мережі пристроїв поза 192.168.88.0/24 (камери замовника на 192.168.15.x тощо).

    Роутер бере в кожній таку адресу .1 (зазвичай це шлюз старої бортової мережі), а якщо її зайняв
    пристрій — .254. Так пристрої не треба переналаштовувати. Мережі Starlink (STARLINK_NET) тут не буває навіть
    зі старого запису: device_ip таких не бере — краще пристрій поза тунелем, ніж НРК без інтернету.
    """
    ips = [device_ip(robot, h) for h in DEVICES]
    nets: list[dict] = []
    for net in dict.fromkeys(ipaddress.ip_network(f"{ip}/24", strict=False) for ip in ips if ip not in LAN):
        router = next(a for a in (net.network_address + 1, net.network_address + 254) if a not in ips)
        nets.append({"net": str(net), "router": str(router), "prefix": net.prefixlen})
    return nets


def operator_ip(site: Site, op: Operator) -> IPv4Address:
    return site.network.operators.network_address + op.id


def arbiter_port(slot: int) -> int:
    return ARBITER_BASE_PORT + slot


def capacity(site: Site) -> int:
    """Скільки НРК вміщується на майданчик."""
    tunnel = site.network.tunnel.num_addresses - 3  # адреса мережі, сервер, broadcast
    blocks = site.network.robot_lans.num_addresses // BLOCK_SIZE
    return max(0, min(MAX_SLOT, tunnel, blocks))


def service_server_ip(svc: Service) -> IPv4Address:
    return svc.network.network_address + 1


def service_engineers_net(svc: Service) -> IPv4Network:
    """Перша /24 сервісної мережі: сервер та інженери. НРК — далі."""
    return ipaddress.ip_network((svc.network.network_address, 24))


def service_engineer_ip(svc: Service, eng: Engineer) -> IPv4Address:
    return svc.network.network_address + 1 + eng.id


def service_robot_ip(svc: Service, robot_id: int) -> IPv4Address:
    return svc.network.network_address + 256 + robot_id
