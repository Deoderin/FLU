"""Пошук роутерів MikroTik у локальній мережі (MNDP — те саме, що список сусідів у WinBox).

Роутер відповідає на широкомовний запит на UDP 5678 своїм MAC, моделлю, версією
RouterOS і IP-адресою. Працює навіть тоді, коли IP роутера не з мережі ноутбука:
MNDP іде на рівні Ethernet. Пароль для пошуку не потрібен.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import struct
import time

from .i18n import T

MNDP_PORT = 5678
TEXT_FIELDS = {5: "identity", 7: "version", 8: "platform", 11: "software_id", 12: "board", 16: "interface"}


def parse_mndp(data: bytes) -> dict | None:
    """Пакет MNDP: 4 байти заголовка, далі поля «тип (2 байти), довжина (2 байти), значення»."""
    if len(data) < 8:
        return None
    out: dict = {}
    i = 4
    while i + 4 <= len(data):
        kind, size = struct.unpack(">HH", data[i:i + 4])
        value = data[i + 4:i + 4 + size]
        i += 4 + size
        if len(value) < size:
            break
        if kind == 1 and size == 6:
            out["mac"] = ":".join(f"{b:02X}" for b in value)
        elif kind == 17 and size == 4:
            out["ipv4"] = str(ipaddress.IPv4Address(value))
        elif kind == 10 and size == 4:
            out["uptime"] = int.from_bytes(value, "little")
        elif kind in TEXT_FIELDS:
            out[TEXT_FIELDS[kind]] = value.decode("utf-8", "replace").strip("\x00")
    if "mac" not in out:
        return None
    out.setdefault("ipv4", None)
    return out


def local_addresses() -> list[str]:
    """IPv4-адреси ноутбука (Ethernet, Wi-Fi, 169.254.x.x без DHCP), крім 127.x."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if not i[4][0].startswith("127.")})


def _udp(bind: tuple[str, int]) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(bind)
    s.setblocking(False)
    return s


def discover(timeout: float = 3.0) -> list[dict]:
    """Роутери MikroTik, що відповіли за timeout секунд (запит повторюється щосекунди).

    Windows шле широкомовний пакет лише через основний інтерфейс (зазвичай Wi-Fi),
    тож запит іде окремо з кожної адреси ноутбука — так він потрапить і в кабель до роутера.
    """
    sockets = []
    try:
        sockets.append(_udp(("", MNDP_PORT)))  # роутери ще й самі шлють анонси на цей порт
    except OSError:
        pass  # порт зайнятий (наприклад, відкрито WinBox) — лишаються відповіді на наші запити
    for ip in local_addresses() or [""]:
        try:
            sockets.append(_udp((ip, 0)))
        except OSError:
            continue
    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    next_ping = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_ping:
                for s in sockets[1:] if len(sockets) > 1 else sockets:
                    try:
                        s.sendto(b"\x00\x00\x00\x00", ("255.255.255.255", MNDP_PORT))
                    except OSError:
                        pass
                next_ping = time.monotonic() + 1
            ready, _, _ = select.select(sockets, [], [], 0.2)
            for s in ready:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:  # «порт недоступний» від Windows — просто чекаємо далі
                    continue
                info = parse_mndp(data)
                if info:
                    info["from"] = addr[0]
                    found[info["mac"]] = info
    finally:
        for s in sockets:
            s.close()
    return merge(found.values(), local_addresses())


def _same_net(ip: str, local: list[str]) -> bool:
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return any(ipaddress.ip_address(a) in net for a in local)


def merge(entries, local: list[str] = ()) -> list[dict]:
    """Один запис на роутер. Роутер відповідає з кожного свого порту (у кожного свій MAC),
    тож hEX, увімкнений двома портами в мережу ноутбука, видно двічі. Ліцензія RouterOS
    (software-id) у них спільна — за нею й склеюємо.

    Адреса для підключення: заводська 192.168.88.x, інакше та, що з мережі ноутбука.
    """
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e.get("software_id") or e["mac"], []).append(e)
    out = []
    for items in groups.values():
        items.sort(key=lambda r: r["mac"])
        ips = list(dict.fromkeys(r["ipv4"] for r in items if r["ipv4"]))
        best = sorted(ips, key=lambda ip: (not ip.startswith("192.168.88."), not _same_net(ip, local)))
        out.append({**items[0], "ipv4": best[0] if best else None,
                    "macs": [r["mac"] for r in items], "ips": best})
    return sorted(out, key=lambda r: r["mac"])


def describe(router: dict) -> str:
    model = router.get("board") or router.get("platform") or "MikroTik"
    parts = [model, "MAC " + ", ".join(router.get("macs") or [router["mac"]])]
    if router.get("version"):
        parts.append(f"RouterOS {router['version'].split()[0]}")
    ips = router.get("ips") or ([router["ipv4"]] if router.get("ipv4") else [])
    parts.append("IP " + ", ".join(ips) if ips else T("без IP", "no IP"))
    return " · ".join(parts)
