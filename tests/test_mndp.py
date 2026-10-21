"""Пошук роутера MikroTik (MNDP), вибір адреси для прошивки й заливка конфігу."""
import struct

import pytest

from fleet import mikrotik, mndp, netcheck
from fleet.mikrotik import MikroTikError, find_bench_router
from fleet.mndp import describe, parse_mndp


def tlv(kind: int, value: bytes) -> bytes:
    return struct.pack(">HH", kind, len(value)) + value


def packet(mac=b"\x48\xa9\x8a\x01\x02\x03", ipv4=b"\xc0\xa8\x58\x01", board=b"hAP ax^2", version=b"7.16.1 (stable)"):
    body = tlv(1, mac) + tlv(5, b"MikroTik") + tlv(7, version) + tlv(8, b"MikroTik") + tlv(10, (3600).to_bytes(4, "little"))
    body += tlv(12, board) + tlv(16, b"bridge")
    if ipv4:
        body += tlv(17, ipv4)
    return b"\x00\x00\x00\x01" + body


def test_parse_packet():
    info = parse_mndp(packet())
    assert info["mac"] == "48:A9:8A:01:02:03" and info["ipv4"] == "192.168.88.1"
    assert info["board"] == "hAP ax^2" and info["uptime"] == 3600 and info["interface"] == "bridge"
    assert describe(info) == "hAP ax^2 · MAC 48:A9:8A:01:02:03 · RouterOS 7.16.1 · IP 192.168.88.1"


def test_parse_router_without_ip_and_garbage():
    info = parse_mndp(packet(ipv4=None))
    assert info["ipv4"] is None and describe(info).endswith("без IP")
    assert parse_mndp(b"\x00\x00\x00\x00") is None  # наш власний запит
    assert parse_mndp(packet()[:-3]) is not None  # обрізаний хвіст не ламає розбір
    assert parse_mndp(b"\x00\x00\x00\x01" + tlv(5, b"no mac")) is None


def test_one_router_seen_from_two_ports():
    # hEX S: ether2 у мережі ноутбука, ether1 теж — два MAC, одна ліцензія RouterOS
    a = {"mac": "D0:EA:11:D3:BB:9E", "software_id": "WGTH-73MG", "ipv4": "192.168.17.1", "board": "E60iUGS"}
    b = {"mac": "D0:EA:11:D3:BB:9F", "software_id": "WGTH-73MG", "ipv4": "192.168.50.126", "board": "E60iUGS"}
    other = {"mac": "48:A9:8A:01:02:03", "software_id": "ABCD-1234", "ipv4": None}
    routers = mndp.merge([b, other, a], local=["192.168.50.100"])
    assert len(routers) == 2
    hex_s = next(r for r in routers if r["software_id"] == "WGTH-73MG")
    assert hex_s["ipv4"] == "192.168.50.126"  # адреса з мережі ноутбука
    assert hex_s["macs"] == ["D0:EA:11:D3:BB:9E", "D0:EA:11:D3:BB:9F"]
    assert "IP 192.168.50.126, 192.168.17.1" in describe(hex_s)
    factory = mndp.merge([a, {**b, "ipv4": "192.168.88.1"}], local=["192.168.17.20"])
    assert factory[0]["ipv4"] == "192.168.88.1"  # заводська адреса важливіша


LINK_OK = {"status": "ok", "message": "кабель під'єднано"}
LINK_STATIC = {"status": "static", "message": "на порту «Ethernet» адреси задано вручну"}


@pytest.fixture(autouse=True)
def cable_ok(monkeypatch):
    """Без справжньої перевірки адаптерів ноутбука: за замовчуванням кабель у порядку."""
    monkeypatch.setattr(netcheck, "check_router_link", lambda adapters=None: LINK_OK)


@pytest.mark.parametrize("routers, expected", [
    ([], "192.168.88.1"),  # нічого не знайшли — заводська адреса
    ([{"mac": "A", "ipv4": "192.168.88.1"}, {"mac": "B", "ipv4": "10.0.0.1"}], "192.168.88.1"),  # плюс роутер у Wi-Fi
    ([{"mac": "A", "ipv4": "192.168.50.1"}], "192.168.50.1"),  # єдиний роутер з іншою адресою
])
def test_router_choice(monkeypatch, routers, expected):
    monkeypatch.setattr(mndp, "discover", lambda timeout=3.0: routers)
    assert find_bench_router(log=lambda *_: None) == expected


def test_cable_problem_is_reported_before_ssh(monkeypatch):
    monkeypatch.setattr(netcheck, "check_router_link", lambda adapters=None: LINK_STATIC)
    for routers in ([], [{"mac": "A", "ipv4": "192.168.88.1"}]):
        monkeypatch.setattr(mndp, "discover", lambda timeout=3.0, r=routers: r)
        with pytest.raises(MikroTikError, match="задано вручну"):
            find_bench_router(log=lambda *_: None)


def test_router_without_ip_or_ambiguous(monkeypatch):
    monkeypatch.setattr(mndp, "discover", lambda timeout=3.0: [{"mac": "A", "ipv4": None}])
    with pytest.raises(MikroTikError, match="немає IP-адреси"):
        find_bench_router(log=lambda *_: None)
    monkeypatch.setattr(mndp, "discover", lambda timeout=3.0: [{"mac": "A", "ipv4": "10.0.0.1"}, {"mac": "B", "ipv4": "10.0.0.2"}])
    with pytest.raises(MikroTikError, match="кілька роутерів"):
        find_bench_router(log=lambda *_: None)
    assert mikrotik.BENCH_HOST == "192.168.88.1"


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeRouter:
    """RouterOS 7: файл, залитий по SFTP, з'являється в /file не одразу."""
    host = "192.168.88.1"

    def __init__(self, appears_after):
        self.appears_after, self.checks, self.commands = appears_after, 0, []

    def has_flash(self):
        return False

    def upload(self, local, remote):
        self.commands.append(f"upload {remote}")

    def get(self, expression):
        self.checks += 1
        return "*1" if self.checks >= self.appears_after else ""

    def run(self, command, **kwargs):
        self.commands.append(command)
        return " 0 name=fleet-robot.rsc type=script size=4.1KiB" if command.startswith("/file print") else ""


def test_reset_waits_for_uploaded_file(monkeypatch):
    monkeypatch.setattr(mikrotik, "time", Clock())
    router = FakeRouter(appears_after=3)
    mikrotik.apply_full_config(router, None)
    assert router.checks == 3
    assert router.commands == ["upload fleet-robot.rsc", "/system reset-configuration no-defaults=yes skip-backup=yes "
                               "run-after-reset=fleet-robot.rsc"]


def test_reset_not_started_without_file(monkeypatch):
    monkeypatch.setattr(mikrotik, "time", Clock())
    router = FakeRouter(appears_after=10_000)
    with pytest.raises(MikroTikError, match="не з'явився в /file") as exc:
        mikrotik.apply_full_config(router, None)
    assert "type=script" in str(exc.value)  # список файлів роутера — для діагностики
    assert not any(c.startswith("/system reset-configuration") for c in router.commands)
