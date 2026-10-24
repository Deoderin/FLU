"""Пристрої на борту: хто в якому порту і на якій адресі — і роутер, прошитий під ці адреси."""
import json
import re

import pytest

from fleet import addressing as A
from fleet import cli, mikrotik
from fleet.acceptance import Acceptance
from fleet.inventory import Inventory
from fleet.mikrotik import MikroTikError
from fleet.mikrotik import detect_devices, parse_bridge_hosts, parse_ip_macs, pick_devices, probe_plan, router_addresses
from fleet.render import Renderer

PORTS = {"cam1": "ether2", "cam2": "ether3", "fc": "ether4", "bench": "ether5"}

# Справжній вивід з НРК замовника: камери й адаптер ФК лишилися в старій бортовій мережі 192.168.15.x
IP_SCAN = """Flags: D - DHCP
Columns: ADDRESS, MAC-ADDRESS
  ADDRESS         MAC-ADDRESS
D 192.168.15.108  40:7A:A4:EC:FF:B7
  192.168.15.253  04:EE:E8:1C:88:7A
D 192.168.15.109  40:7A:A4:EC:FD:77
"""
BRIDGE_HOSTS = """ 0   D mac-address=40:7A:A4:EC:FF:B7 on-interface=ether2 bridge=br-lan
 1   D mac-address=40:7A:A4:EC:FD:77 on-interface=ether3 bridge=br-lan
 2   D mac-address=04:EE:E8:1C:88:7A on-interface=ether4 bridge=br-lan
 3   D mac-address=3C:7C:3F:00:11:22 on-interface=ether5 bridge=br-lan
"""
ARP = " 0 DC address=192.168.88.254 mac-address=3C:7C:3F:00:11:22 interface=br-lan status=reachable\n"
CUSTOMER = {"cam1": {"ip": "192.168.15.108", "mac": "40:7A:A4:EC:FF:B7"},
            "cam2": {"ip": "192.168.15.109", "mac": "40:7A:A4:EC:FD:77"},
            "fc": {"ip": "192.168.15.253", "mac": "04:EE:E8:1C:88:7A"}}


def test_parse_router_output():
    assert parse_bridge_hosts(BRIDGE_HOSTS)["ether2"] == ["40:7A:A4:EC:FF:B7"]
    ip_macs = parse_ip_macs(IP_SCAN + ARP)
    assert ip_macs["40:7A:A4:EC:FD:77"] == ["192.168.15.109"]
    assert ip_macs["3C:7C:3F:00:11:22"] == ["192.168.88.254"]  # ARP: address=..., не mac-address=...


def test_devices_found_by_port():
    """Камери розрізняємо за портом: адреса нічого не каже, яка з них передня."""
    found = pick_devices(PORTS, parse_bridge_hosts(BRIDGE_HOSTS), parse_ip_macs(IP_SCAN + ARP),
                         own={"192.168.88.1"}, log=lambda *_: None)
    assert found == CUSTOMER


def test_cameras_on_one_address_are_not_used():
    """Дві камери на заводській 192.168.1.108: розвести їх роутер не може — кажемо, а адреси не записуємо."""
    scan = "192.168.1.108  40:7A:A4:EC:FF:B7\n192.168.1.108  40:7A:A4:EC:FD:77\n"
    logged = []
    found = pick_devices(PORTS, parse_bridge_hosts(BRIDGE_HOSTS), parse_ip_macs(scan), own=set(), log=logged.append)
    assert "cam1" not in found and "cam2" not in found
    assert any("на одній адресі 192.168.1.108" in line for line in logged)


class Router:
    """Заводський роутер: міст bridge з 192.168.88.1, Starlink на ether1 (192.168.1.x).

    heard — що чути, просто слухаючи мережу; probe — хто відповідає на активний опит своєї мережі /24.
    """

    def __init__(self, heard=IP_SCAN, probe=None, hosts=BRIDGE_HOSTS, links=None):
        self.heard, self.probe, self.hosts = heard, probe or {}, hosts
        self.links = links or {}
        self.commands = []
        self.temp = []

    def get(self, expr):
        if "running" in expr:
            port = re.search(r"name=(\w+)", expr)[1]
            return "true" if self.links.get(port, True) else "false"
        return "bridge"

    def run(self, command, check=True, timeout=60):
        self.commands.append(command)
        if command.startswith("/interface bridge host"):
            return self.hosts
        if command.startswith("/tool ip-scan") and "address-range" in command:
            return self.probe.get(re.search(r"address-range=(\S+)", command)[1], "")
        if command.startswith("/tool ip-scan"):
            return self.heard
        if command.startswith("/ip arp"):
            return ARP
        if command.startswith("/ip address add"):
            self.temp.append(re.search(r"address=(\S+)", command)[1])
            return ""
        if command.startswith("/ip address remove"):
            self.temp = []
            return ""
        return ("  0   address=192.168.88.1/24 network=192.168.88.0 interface=bridge actual-interface=bridge\n"
                "  1 D address=192.168.1.37/24 network=192.168.1.0 interface=ether1 actual-interface=ether1\n"
                + "".join(f"  2   address={a} interface=bridge actual-interface=bridge\n" for a in self.temp))


def test_detect_devices_asks_the_router():
    router = Router()
    assert detect_devices(router, PORTS, log=lambda *_: None) == CUSTOMER
    assert "/tool ip-scan interface=bridge duration=10s" in router.commands
    assert not any("address-range" in c or "address add" in c for c in router.commands)  # усіх почули — опит не потрібен


def test_silent_camera_is_asked():
    """Задня камера, яку ніхто не дивиться, мовчить. Її мережу знаємо з передньої камери — питаємо самі."""
    heard = "192.168.15.108  40:7A:A4:EC:FF:B7\n192.168.15.253  04:EE:E8:1C:88:7A\n"
    router = Router(heard=heard, probe={"192.168.15.0/24": "192.168.15.109  40:7A:A4:EC:FD:77\n"})
    logged = []
    assert detect_devices(router, PORTS, log=logged.append, hints=["192.168.1.108"]) == CUSTOMER
    adds = [c for c in router.commands if c.startswith("/ip address add")]
    assert adds == ["/ip address add address=192.168.15.1/24 interface=bridge comment=fleet-probe"]
    assert "/ip address remove [find comment=fleet-probe]" in router.commands and router.temp == []
    scans = [c for c in router.commands if "address-range" in c]
    assert any("address-range=192.168.15.0/24" in c for c in scans)
    assert any("address-range=192.168.88.0/24" in c for c in scans)  # своя мережа роутера — без тимчасової адреси
    # 192.168.1.0/24 (заводська мережа камер) — це Starlink на ether1: тимчасова адреса на мосту зламала б маршрут
    assert not any("192.168.1.0/24" in c for c in scans)
    assert any("задня камера (ether3): 192.168.15.109" in line for line in logged)


def test_no_link_is_explained_without_waiting():
    hosts = "\n".join(line for line in BRIDGE_HOSTS.splitlines() if "ether4" not in line)
    router = Router(hosts=hosts, links={"ether4": False})
    logged = []
    found = detect_devices(router, PORTS, log=logged.append)  # чекати нема кого: лінку немає
    assert "fc" not in found and found["cam1"] == CUSTOMER["cam1"]
    assert any("ФК (ether4): у порту немає лінку" in line for line in logged)


def test_probe_plan_keeps_off_other_interfaces():
    addresses = router_addresses("  0   address=192.168.88.1/24 network=192.168.88.0 interface=br-lan actual-interface=br-lan\n"
                                 "  1 D address=192.168.1.37/24 network=192.168.1.0 interface=ether1\n"
                                 "  2   address=10.150.0.2/16 network=10.150.0.0 interface=wg-main\n")
    plan = probe_plan("br-lan", addresses, ["192.168.88.1", "192.168.1.108", "10.150.16.8", "192.168.15.108", "8.8.8.8"],
                      taken={"192.168.15.1", "192.168.15.108"})
    assert [(str(net), temp) for net, temp in plan] == [
        ("192.168.88.0/24", None),             # роутер уже в ній
        ("192.168.15.0/24", "192.168.15.254"),  # .1 зайнята пристроєм
    ]


def test_router_config_for_customer_devices(root, provisioned, store):
    """Роутер бере адресу в мережі пристроїв, веде до них тунель і закріплює їхні адреси в DHCP."""
    provisioned.set_devices(1, CUSTOMER)
    provisioned.save_robots()
    inv = Inventory.load(root)
    robot = inv.robot(1)
    assert robot.devices == CUSTOMER and inv.validate() == []
    rsc, _ = Renderer(inv, store).robot_rsc(robot, inv.site(robot.site), robot.slot)
    assert "/ip address add address=192.168.15.1/24 interface=br-lan" in rsc
    nat = [re.search(r"dst-address=(\S+) action=(\S+) to-addresses=(\S+)", n).groups()
           for n in rsc.splitlines() if n.startswith("/ip firewall nat add chain=dstnat")]
    assert nat == [
        ("10.150.16.2", "dst-nat", "192.168.15.253"),  # ФК поза netmap: свій dst-nat
        ("10.150.16.8", "dst-nat", "192.168.15.108"),
        ("10.150.16.9", "dst-nat", "192.168.15.109"),
        ("10.150.16.0/28", "netmap", "192.168.88.0/28"),
    ]
    assert "lease add server=fleet-lan mac-address=40:7A:A4:EC:FF:B7 address=192.168.15.108" in rsc
    assert "dhcp-server network add address=192.168.15.0/24 gateway=192.168.15.1" in rsc
    assert 'dst-address=192.168.15.253 comment="fleet: fc' in rsc
    assert "dst-address=192.168.15.0/24 protocol=icmp" in rsc


def test_standard_addresses_are_not_stored(root, provisioned):
    """Пристрої на стандартних 192.168.88.x у robots.yaml не пишемо; знайдені знову — прибираємо старий запис."""
    provisioned.set_devices(1, {"cam1": {"ip": "192.168.15.108", "mac": "40:7A:A4:EC:FF:B7"}})
    provisioned.set_devices(1, {"cam1": {"ip": "192.168.88.108", "mac": "40:7A:A4:EC:FF:B7"}})
    provisioned.save_robots()
    assert Inventory.load(root).robot(1).devices == {}
    assert "devices" not in (root / "inventory" / "robots.yaml").read_text("utf-8")


def test_router_takes_254_when_1_is_a_device(provisioned):
    robot = provisioned.robot(1)
    robot.devices = {"fc": {"ip": "192.168.15.1"}}
    assert A.device_nets(robot) == [{"net": "192.168.15.0/24", "router": "192.168.15.254", "prefix": 24}]


class Flashed(Exception):
    """Дійшло до заливання конфігу — далі тест не йде (перезавантаження роутера не імітуємо)."""


@pytest.fixture
def bench(root, provisioned, monkeypatch):
    """fleet bench mikrotik з роутером-заглушкою: пошук пристроїв повертає found, заливання — виняток Flashed."""
    class Mt:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    state = {"found": {}}
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(mikrotik, "open_router", lambda host, passwords: Mt())
    monkeypatch.setattr(mikrotik, "detect_devices", lambda mt, ports, hints=(): state["found"])

    def apply(mt, rsc):
        raise Flashed()
    monkeypatch.setattr(mikrotik, "apply_full_config", apply)

    def flash(*extra):
        args = cli.build_parser().parse_args(["bench", "mikrotik", "--id", "1", "--host", "192.168.88.1", *extra])
        return args.fn(args)
    return state, flash


def test_flash_stops_when_devices_are_missing(root, bench):
    """Задньої камери й ФК не знайшли: роутер не прошиваємо — інакше тунель вів би в нікуди, а тест мовчав."""
    state, flash = bench
    state["found"] = {"cam1": CUSTOMER["cam1"]}
    assert flash() == cli.EXIT_DEVICES_MISSING
    scan = json.loads((root / "build" / "robots" / "nrk-001" / "devices.json").read_text("utf-8"))
    assert scan["stopped"] is True and scan["via"] == "bench"
    assert [(d["host"], d["port"], d["status"]) for d in scan["devices"]] == [
        ("fc", "ether4", "missing"), ("cam1", "ether2", "found"), ("cam2", "ether3", "missing")]
    with pytest.raises(Flashed):  # «Прошити без них»
        flash("--allow-missing")
    assert json.loads((root / "build" / "robots" / "nrk-001" / "devices.json").read_text("utf-8"))["stopped"] is False


def test_previous_address_is_enough(root, bench):
    """Пристрій мовчить, але його адреса відома з минулої прошивки — прошиваємо з нею."""
    state, flash = bench
    inv = Inventory.load(root)
    inv.set_devices(1, {"fc": CUSTOMER["fc"], "cam2": CUSTOMER["cam2"]})
    inv.save_robots()
    state["found"] = {"cam1": CUSTOMER["cam1"]}
    with pytest.raises(Flashed):
        flash()
    scan = json.loads((root / "build" / "robots" / "nrk-001" / "devices.json").read_text("utf-8"))
    assert {d["host"]: (d["status"], d["ip"]) for d in scan["devices"]}["fc"] == ("kept", "192.168.15.253")


def test_tunnel_reflash_needs_a_hub(bench):
    _, flash = bench
    with pytest.raises(MikroTikError, match="лише НРК, зареєстрований на hub"):
        flash("--tunnel")


def test_acceptance_pings_devices_from_the_router(provisioned, store):
    """Приймальний тест: камери й ФК відповідають роутеру там, куди веде тунель."""
    class Mt:
        def run(self, command, check=True, timeout=60):
            if command == "/ping 192.168.88.2 count=3":
                return "  SEQ HOST  SIZE TTL TIME\n    0 192.168.88.2 56 64 1ms\n    sent=3 received=3 packet-loss=0%\n"
            if command.startswith("/ping"):
                return "    0 192.168.88.10x  host unreachable\n    sent=3 received=0 packet-loss=100%\n"
            if "address=192.168.88.108" in command:  # камера ігнорує ICMP, але на ARP відповідає
                return " 0 DC address=192.168.88.108 mac-address=40:7A:A4:EC:FF:B7 interface=br-lan status=reachable\n"
            return " 0    address=192.168.88.109 interface=br-lan status=failed\n"

    robot = provisioned.robot(1)
    acc = Acceptance(provisioned, store, robot)
    acc.site = provisioned.site(robot.site)
    acc.check_devices(Mt())
    status = {c.key: (c.status, c.detail) for c in acc.checks}
    assert status["dev_fc"][0] == "PASS" and status["dev_cam1"][0] == "PASS" and "ARP" in status["dev_cam1"][1]
    assert status["dev_cam2"][0] == "FAIL" and "порту 3" in status["dev_cam2"][1]
