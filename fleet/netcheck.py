"""Перевірка кабелю до роутера: чи отримав ноутбук адресу від скинутого MikroTik.

Скинутий роутер роздає адреси 192.168.88.x (сам він — 192.168.88.1). Якщо такої адреси
на Ethernet немає, прошивка не дістанеться до роутера — краще сказати це одразу й людською мовою.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess

from . import NO_WINDOW
from .addressing import in_starlink_net
from .i18n import T

ROUTER_NET = ipaddress.ip_network("192.168.88.0/24")

# Дротові адаптери (802.3), їхній стан, DHCP і адреси IPv4. Не -Physical: USB-Ethernet перехідник
# Windows не вважає «фізичним» (ConnectorPresent=False), а PhysicalMediaType у нього буває Unspecified.
# Справжній адаптер сидить на шині PCI чи USB; віртуальні (Hamachi, WireGuard, VPN) — ні.
PS_ADAPTERS = r"""
$ErrorActionPreference = 'SilentlyContinue'
@(Get-NetAdapter | Where-Object { $_.MediaType -eq '802.3' -and $_.PhysicalMediaType -notmatch '802\.11|Wireless|BlueTooth' -and
                                  -not $_.Virtual -and ($_.HardwareInterface -or $_.PnPDeviceID -match '^(USB|PCI)\\') } | ForEach-Object {
  $if = Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4
  $ip = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4
  [pscustomobject]@{ name = $_.Name; up = ($_.Status -eq 'Up'); dhcp = ($if.Dhcp -eq 'Enabled');
                     ips = @($ip | ForEach-Object { $_.IPAddress }) }
}) | ConvertTo-Json -Compress -Depth 3
"""


def wired_adapters() -> list[dict] | None:
    """Дротові адаптери ноутбука. None — визначити не вдалося (не Windows або помилка)."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_ADAPTERS],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                           creationflags=NO_WINDOW)
        data = json.loads(r.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):  # один адаптер PowerShell повертає не списком
        data = [data]
    for a in data:
        ips = a.get("ips") or []
        a["ips"] = [ips] if isinstance(ips, str) else list(ips)
    return data


def check_router_link(adapters: list[dict] | None = None) -> dict:
    """status: ok | no_adapter | no_cable | waiting | static | foreign | unknown, плюс зрозуміле повідомлення."""
    if adapters is None:
        adapters = wired_adapters()
    if adapters is None:
        from .mndp import local_addresses

        mine = [ip for ip in local_addresses() if ipaddress.ip_address(ip) in ROUTER_NET]
        if mine:
            return {"status": "ok", "ip": mine[0], "message": T(f"ноутбук у мережі роутера: {mine[0]}",
                                                               f"the laptop is in the router network: {mine[0]}")}
        return {"status": "unknown", "message": T("не вдалося перевірити мережевий порт ноутбука",
                                                  "could not check the laptop network port")}

    if not adapters:
        return {"status": "no_adapter", "message": T(
            "не бачу дротового порту Ethernet. Під'єднайте USB-Ethernet перехідник (зачекайте, поки Windows його встановить) "
            "і кабель від роутера в порт 5",
            "no wired Ethernet port found. Plug in a USB-Ethernet adapter (wait until Windows installs it) "
            "and the cable from the router into port 5")}
    up = [a for a in adapters if a["up"]]
    if not up:
        return {"status": "no_cable", "message": T(
            "кабель не під'єднано або роутер вимкнено: увімкніть роутер і вставте кабель у порт 5",
            "no cable or the router is off: power on the router and plug the cable into port 5")}
    for a in up:
        for ip in a["ips"]:
            if ipaddress.ip_address(ip) in ROUTER_NET:
                return {"status": "ok", "ip": ip, "adapter": a["name"], "message": T(
                    f"кабель під'єднано, роутер видав ноутбуку адресу {ip} ✓",
                    f"the cable is connected, the router gave the laptop address {ip} ✓")}
    static = [a for a in up if not a["dhcp"]]
    if static and not any(a["dhcp"] for a in up):
        a = static[0]
        listed = ", ".join(a["ips"]) or T("немає", "none")
        return {"status": "static", "adapter": a["name"], "message": T(
            f"на порту «{a['name']}» адреси задано вручну ({listed}). Роутер роздає адресу сам: у налаштуваннях "
            f"мережі для «{a['name']}» увімкніть «Отримувати IP-адресу автоматично» або додайте адресу 192.168.88.250",
            f"addresses on port \"{a['name']}\" are set manually ({listed}). The router assigns an address itself: in the "
            f"network settings of \"{a['name']}\" turn on \"Obtain an IP address automatically\" or add address 192.168.88.250")}
    # Адресу видав хтось інший: роутер зі старим конфігом працює як світч і пропускає DHCP домашньої мережі
    leased = [ip for a in up if a["dhcp"] for ip in a["ips"] if not ip.startswith("169.254.")]
    if leased and in_starlink_net(leased[0]):
        # Starlink за мостом роутера НРК (у порту 2–5 чи через свитч у них) — DHCP Starlink доходить до ноутбука
        return {"status": "foreign", "ip": leased[0], "message": T(
            f"кабель під'єднано, але ноутбук отримав адресу {leased[0]} — найімовірніше від Starlink (він роздає 192.168.1.x), а не від "
            f"роутера НРК: Starlink підключено не в порт 1 роутера, а в порти 2–5 (чи у свитч на них), або кабель ноутбука "
            f"йде в Starlink. Starlink — лише в порт 1, ноутбук — у порт 5",
            f"the cable is connected, but the laptop got address {leased[0]} - most likely from Starlink (it gives 192.168.1.x), not from "
            f"the UGV router: Starlink is plugged not into router port 1 but into ports 2-5 (or a switch on them), or the "
            f"laptop cable goes to Starlink. Starlink goes only into port 1, the laptop into port 5")}
    if leased:
        return {"status": "foreign", "ip": leased[0], "message": T(
            f"кабель під'єднано, але ноутбук отримав адресу {leased[0]} не від скинутого роутера (той роздає 192.168.88.x): "
            f"роутер не скинуто до заводських налаштувань або кабель іде не до нього",
            f"the cable is connected, but the laptop got address {leased[0]} not from a reset router (it gives 192.168.88.x): "
            f"the router is not reset to factory settings or the cable does not go to it")}
    return {"status": "waiting", "message": T(
        "кабель під'єднано, але роутер ще не видав адресу. Зачекайте 30 секунд після ввімкнення роутера. "
        "Якщо не допоможе — роутер не скинуто до заводських налаштувань або кабель у порту 1",
        "the cable is connected but the router has not given an address yet. Wait 30 seconds after the router "
        "powers on. If that does not help, the router is not reset to factory settings or the cable is in port 1")}
