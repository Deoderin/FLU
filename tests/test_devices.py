"""Пристрої на борту: хто в якому порту і на якій адресі — і роутер, прошитий під ці адреси."""
import re

from fleet import addressing as A
from fleet.inventory import Inventory
from fleet.mikrotik import detect_devices, parse_bridge_hosts, parse_ip_macs, pick_devices
from fleet.render import Renderer

PORTS = {"cam1": "ether2", "cam2": "ether3", "fc": "ether4", "bench": "ether5"}

# Справжній вивід з НРК заказника: камери й адаптер ФК лишилися в старій бортовій мережі 192.168.15.x
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


def test_detect_devices_asks_the_router():
    class Router:
        def __init__(self):
            self.commands = []

        def get(self, expr):
            return "bridge"  # заводський конфіг: міст зветься bridge

        def run(self, command, check=True, timeout=60):
            self.commands.append(command)
            if command.startswith("/interface bridge host"):
                return BRIDGE_HOSTS
            if command.startswith("/tool ip-scan"):
                return IP_SCAN
            if command.startswith("/ip arp"):
                return ARP
            return " 0   address=192.168.88.1/24 network=192.168.88.0 interface=bridge\n"

    router = Router()
    assert detect_devices(router, PORTS, log=lambda *_: None) == CUSTOMER
    assert "/tool ip-scan interface=bridge duration=10s" in router.commands


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
