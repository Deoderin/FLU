"""Камери на стенді: перенесення із заводської IP-адреси на адресу НРК.

Обидві камери приходять з однаковою заводською IP-адресою, тому розрізняємо їх
за портом MikroTik: поки налаштовується камера 1, порт камери 2 вимкнено, і навпаки.
Камеру має бути активовано (SADP / ConfigTool) паролем, який видав
fleet provision: активацію й зміну пароля скрипт не робить.
"""
from __future__ import annotations

import ipaddress
import time
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from . import addressing as A
from .i18n import T
from .inventory import Inventory, Robot
from .keystore import SecretStore
from .mikrotik import MikroTik

BENCH_TAG = "fleet-bench"


class CameraError(Exception):
    pass


def wrong_password(base: str) -> CameraError:
    return CameraError(T(f"{base}: камера відповідає, але не приймає пароль — активована іншим паролем?",
                         f"{base}: the camera answers but rejects the password - activated with a different one?"))


class Hikvision:
    NET_PATH = "/ISAPI/System/Network/interfaces/1/ipAddress"

    def __init__(self, ip: str, user: str, password: str, timeout: float = 5):
        self.base = f"http://{ip}"
        self.auth = HTTPDigestAuth(user, password)
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            r = requests.get(self.base + "/ISAPI/System/deviceInfo", auth=self.auth, timeout=self.timeout)
        except requests.RequestException:
            return False
        if r.status_code == 401:
            raise wrong_password(self.base)
        return r.ok

    def set_address(self, ip: str, mask: str, gateway: str) -> None:
        r = requests.get(self.base + self.NET_PATH, auth=self.auth, timeout=self.timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
        q = (lambda tag: f"{{{ns}}}{tag}") if ns else (lambda tag: tag)
        values = {q("addressingType"): "static", q("ipAddress"): ip, q("subnetMask"): mask}
        for el in root:
            if el.tag in values:
                el.text = values[el.tag]
        gw = root.find(f"{q('DefaultGateway')}/{q('ipAddress')}")
        if gw is not None:
            gw.text = gateway
        if ns:
            ET.register_namespace("", ns)
        try:
            r = requests.put(self.base + self.NET_PATH, data=ET.tostring(root), auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            pass  # камера змінила адресу й обірвала відповідь — перевіримо за новою адресою


class Dahua:
    def __init__(self, ip: str, user: str, password: str, timeout: float = 5):
        self.base = f"http://{ip}"
        self.auth = HTTPDigestAuth(user, password)
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            r = requests.get(self.base + "/cgi-bin/magicBox.cgi?action=getSystemInfo", auth=self.auth, timeout=self.timeout)
        except requests.RequestException:
            return False
        if r.status_code == 401:
            raise wrong_password(self.base)
        return r.ok

    def set_address(self, ip: str, mask: str, gateway: str) -> None:
        # У деяких моделей інтерфейс називається eth2, а не eth0: перевірте на своїй камері
        params = {
            "action": "setConfig",
            "Network.eth0.IPAddress": ip,
            "Network.eth0.SubnetMask": mask,
            "Network.eth0.DefaultGateway": gateway,
            "Network.eth0.DhcpEnable": "false",
        }
        try:
            r = requests.get(self.base + "/cgi-bin/configManager.cgi", params=params, auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            pass


VENDORS = {"hikvision": Hikvision, "dahua": Dahua}


def setup_cameras(inv: Inventory, store: SecretStore, robot: Robot, mt: MikroTik, log=print) -> None:
    plat = inv.platform(robot)
    cfg, ports = plat["cameras"], plat["ports"]
    vendor = VENDORS.get(cfg["vendor"])
    if vendor is None:
        raise CameraError(T(f"невідомий виробник камер {cfg['vendor']}: підтримуються {', '.join(VENDORS)}",
                            f"unknown camera vendor {cfg['vendor']}: supported {', '.join(VENDORS)}"))
    user, password = cfg["factory_user"], store.robot(robot.id)["camera_password"]
    factory = ipaddress.ip_address(cfg["factory_ip"])
    factory_net = ipaddress.ip_network(f"{factory}/24", strict=False)
    helper = factory_net.network_address + (251 if int(factory) & 255 == 250 else 250)
    mask, gateway = "255.255.255.0", str(A.lan_ip("router"))
    cam_ports = {cam: ports[cam] for cam in A.CAMERAS}

    # Тимчасова адреса в заводській мережі та NAT: стенд за MikroTik дістає до камери,
    # а камера відповідає MikroTik, не знаючи маршруту до 192.168.88.0/24
    mt.run(f'/ip address add address={helper}/24 interface=br-lan comment="{BENCH_TAG}"')
    mt.run(f'/ip firewall nat add chain=srcnat dst-address={factory_net} action=masquerade comment="{BENCH_TAG}"')
    try:
        for cam in A.CAMERAS:
            target = str(A.lan_ip(cam))
            if vendor(target, user, password).ping():
                log(T(f"{cam}: вже на {target}", f"{cam}: already at {target}"))
                continue
            others = [p for c, p in cam_ports.items() if c != cam]
            for p in others:
                mt.run(f"/interface disable {p}")
            mt.run(f"/interface enable {cam_ports[cam]}")
            mt.run(f'/ip arp remove [find address="{factory}"]', check=False)
            time.sleep(3)
            cam_api = vendor(str(factory), user, password)
            if not cam_api.ping():
                raise CameraError(T(
                    f"{cam}: не відповідає на {factory} через порт {cam_ports[cam]}. Перевірте кабель і що камеру "
                    f"активовано паролем із secrets/robots/{robot.id}.json (camera_password)",
                    f"{cam}: no answer at {factory} on port {cam_ports[cam]}. Check the cable and that the camera "
                    f"is activated with the password from secrets/robots/{robot.id}.json (camera_password)",
                ))
            log(f"{cam}: {factory} -> {target}")
            cam_api.set_address(target, mask, gateway)
            for p in others:
                mt.run(f"/interface enable {p}")
            deadline = time.monotonic() + 90
            while not vendor(target, user, password).ping():
                if time.monotonic() > deadline:
                    raise CameraError(T(f"{cam}: після зміни адреси камера не з'явилася на {target}",
                                        f"{cam}: the camera did not show up at {target} after the address change"))
                time.sleep(3)
            log(T(f"{cam}: готово", f"{cam}: done"))
    finally:
        for p in cam_ports.values():
            mt.run(f"/interface enable {p}", check=False)
        mt.run(f'/ip firewall nat remove [find comment="{BENCH_TAG}"]', check=False)
        mt.run(f'/ip address remove [find comment="{BENCH_TAG}"]', check=False)
