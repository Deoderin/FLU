"""Адресный план: все адреса робота выводятся из площадки и его slot.

Внутри робота сеть всегда одна и та же (192.168.88.0/24). Снаружи, через туннель
площадки, робот виден блоком /28 из robot_lans: MikroTik делает netmap
блок -> 192.168.88.0/28, поэтому адрес устройства = начало блока + номер хоста.
"""
from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address, IPv4Network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inventory import Engineer, Operator, Service, Site

LAN = ipaddress.ip_network("192.168.88.0/24")
LAN_MAPPED = ipaddress.ip_network("192.168.88.0/28")
HOSTS = {"router": 1, "fc": 2, "cam1": 11, "cam2": 12}
CAMERAS = ("cam1", "cam2")
BLOCK_PREFIX = LAN_MAPPED.prefixlen
BLOCK_SIZE = LAN_MAPPED.num_addresses
MAX_SLOT = 250  # slot — это MAVLink SYSID; 255 занят наземной станцией

ARBITER_BASE_PORT = 5760  # оператор подключается к роботу со slot N на порт 5760 + N
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
    return lan_block(site, slot).network_address + HOSTS[host]


def operator_ip(site: Site, op: Operator) -> IPv4Address:
    return site.network.operators.network_address + op.id


def arbiter_port(slot: int) -> int:
    return ARBITER_BASE_PORT + slot


def capacity(site: Site) -> int:
    """Сколько роботов помещается на площадку."""
    tunnel = site.network.tunnel.num_addresses - 3  # адрес сети, сервер, broadcast
    blocks = site.network.robot_lans.num_addresses // BLOCK_SIZE
    return max(0, min(MAX_SLOT, tunnel, blocks))


def service_server_ip(svc: Service) -> IPv4Address:
    return svc.network.network_address + 1


def service_engineers_net(svc: Service) -> IPv4Network:
    """Первая /24 сервисной сети: сервер и инженеры. Роботы — дальше."""
    return ipaddress.ip_network((svc.network.network_address, 24))


def service_engineer_ip(svc: Service, eng: Engineer) -> IPv4Address:
    return svc.network.network_address + 1 + eng.id


def service_robot_ip(svc: Service, robot_id: int) -> IPv4Address:
    return svc.network.network_address + 256 + robot_id
