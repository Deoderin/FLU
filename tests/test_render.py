import re

import pytest
from ruamel.yaml import YAML

from fleet import addressing as A
from fleet.deploy import DeployError, ensure_site_key
from fleet.keystore import wg_pubkey
from fleet.mikrotik import drift, parse_ros_duration
from fleet.provision import provision_robot
from fleet.render import Renderer

safe_yaml = YAML(typ="safe", pure=True)


def test_provision_assigns_slot_and_keys(provisioned, store, root):
    r1 = provisioned.robot(1)
    assert (r1.site, r1.slot) == ("flu-acme", 1)
    assert store.has_site("flu-acme") and store.has_site("globex") and store.has_service()
    assert not store.has_site("initech")  # сервер замовника — не наш, ключа немає
    assert (root / "build" / "robots" / "nrk-001" / "robot.rsc").exists()


def test_provision_is_idempotent(provisioned, store):
    key = store.robot(1)["wg_site"]
    provision_robot(provisioned, store, provisioned.robot(1))
    assert provisioned.robot(1).slot == 1
    assert store.robot(1)["wg_site"] == key


def test_robot_and_server_keys_match(provisioned, store):
    r = Renderer(provisioned, store)
    robot = provisioned.robot(1)
    site = provisioned.site("flu-acme")
    rsc, _ = r.robot_rsc(robot, site, robot.slot)
    server_pub = wg_pubkey(store.site("flu-acme")["wg"])
    assert f'public-key="{server_pub}"' in rsc

    r.render_site(site)
    conf = (r.site_dir(site) / "server" / "wg-flu-acme.conf").read_text("utf-8")
    assert f"PublicKey = {wg_pubkey(store.robot(1)['wg_site'])}" in conf
    assert "AllowedIPs = 10.150.0.2/32, 10.150.16.0/28" in conf


def test_robot_rsc_is_ascii_and_complete(provisioned, store):
    r = Renderer(provisioned, store)
    for robot in provisioned.robots.values():
        rsc, fp = r.robot_rsc(robot, provisioned.site(robot.site), robot.slot)
        assert rsc.isascii()
        assert f'comment="fleet {fp}"' in rsc
        assert "to-addresses=192.168.88.0/28" in rsc
    rsc3, _ = r.robot_rsc(provisioned.robot(3), provisioned.site("initech"), 1)
    assert "wg-svc" not in rsc3  # замовник не дав сервісний тунель
    assert "fleet-mgmt" in rsc3 and "list=fleet-mgmt address" not in rsc3  # ssh з їхнього сервера не відкриваємо


def test_fingerprint_is_stable_and_tracks_changes(provisioned, store):
    r = Renderer(provisioned, store)
    robot, site = provisioned.robot(1), provisioned.site("flu-acme")
    _, fp1 = r.robot_rsc(robot, site, 1)
    _, fp2 = r.robot_rsc(robot, site, 1)
    assert fp1 == fp2
    provisioned.platforms["flu-v1"]["fc"]["mavlink_port"] = 5762
    _, fp3 = r.robot_rsc(robot, site, 1)
    assert fp3 != fp1


def test_server_bundle(provisioned, store):
    r = Renderer(provisioned, store)
    site = provisioned.site("flu-acme")
    r.render_site(site)
    server = r.site_dir(site) / "server"
    arbiter = safe_yaml.load((server / "config" / "arbiter.yaml").read_text("utf-8"))
    assert arbiter["bind"] == "10.150.0.1"
    assert arbiter["robots"] == [
        {"name": "nrk-001", "slot": 1, "host": "10.150.16.2", "port": 5760, "listen": 5761, "hold_mode": 4}
    ]
    assert {o["name"] for o in arbiter["operators"]} == {"dispatcher-acme", "ivanenko", "petrenko", "test-bench"}

    mtx = safe_yaml.load((server / "config" / "mediamtx.yml").read_text("utf-8"))
    cam = mtx["paths"]["nrk-001-cam1"]
    assert cam["sourceOnDemand"] is True
    assert re.fullmatch(r"rtsp://admin:\w+@10\.150\.16\.8:554/Streaming/Channels/102", cam["source"])
    assert mtx["rtspAddress"] == "10.150.0.1:8554"

    for name in ("docker-compose.yml", "install.sh", "arbiter/arbiter.py", "arbiter/Dockerfile"):
        assert (server / name).exists()
    assert b"\r\n" not in (server / "install.sh").read_bytes()


def test_operator_config_only_reaches_server(provisioned, store):
    r = Renderer(provisioned, store)
    site = provisioned.site("flu-acme")
    r.render_site(site)
    conf = (r.site_dir(site) / "operators" / "ivanenko.conf").read_text("utf-8")
    assert "Address = 10.150.1.10/32" in conf
    assert "AllowedIPs = 10.150.0.1/32" in conf


def test_integration_kit(provisioned, store):
    r = Renderer(provisioned, store)
    readme, peers, creds = r.render_kit(provisioned.site("initech"))
    assert "AllowedIPs = 172.20.5.2/32, 172.20.5.64/28" in peers.read_text("utf-8")
    assert "172.20.5.66:5760" in readme.read_text("utf-8")
    assert creds.read_text("utf-8").count("nrk-003") == 2


def test_integration_kit_in_english(provisioned, store):
    from fleet.i18n import set_lang

    set_lang("en")
    readme, _, _ = Renderer(provisioned, store).render_kit(provisioned.site("initech"))
    text = readme.read_text("utf-8")
    assert text.startswith("# Connecting FLU UGVs") and "НРК" not in text


def test_move_scripts(provisioned, store):
    r = Renderer(provisioned, store)
    robot = provisioned.robot(1)
    add, commit = r.render_move(robot, provisioned.site("globex"), 2, rollback_min=10)
    add_text, commit_text = add.read_text("utf-8"), commit.read_text("utf-8")
    assert add_text.isascii() and commit_text.isascii()
    assert 'comment="fleet-site:globex pending"' in add_text
    assert "name=wg-globex" in add_text and "dst-address=10.160.16.16/28" in add_text
    assert ":delay 10m" in add_text and "fleet-rollback-globex" in add_text
    # підтвердження: спочатку знімаємо pending, потім видаляємо старий майданчик
    assert commit_text.index('comment="fleet-site:globex"') < commit_text.index('remove [find comment="fleet-site:flu-acme"]')
    _, fp_after = r.robot_rsc(robot, provisioned.site("globex"), 2)
    assert f'comment="fleet {fp_after}"' in commit_text


def test_lost_site_key_is_not_silently_regenerated(provisioned, store, root):
    (root / "secrets" / "sites" / "flu-acme.json").unlink()
    with pytest.raises(DeployError, match="Відновіть secrets"):
        ensure_site_key(provisioned, store, provisioned.site("flu-acme"))


def test_render_all(provisioned, store):
    paths, notes = Renderer(provisioned, store).render_all()
    assert len(paths) > 30
    assert notes == []


@pytest.mark.parametrize("text, seconds", [
    ("00:01:05", 65), ("1d02:03:04", 93784), ("1m5s", 65), ("35s", 35), ("1w", 604800), ("", None), ("never", None),
])
def test_parse_ros_duration(text, seconds):
    assert parse_ros_duration(text) == seconds


def test_drift():
    assert drift("a\nb\n", "a\nb\n") == []
    assert drift("a\nb\n", "a\nc\n") == ["- b", "+ c"]


def test_drift_ignores_what_routeros_changes_itself():
    """Еталон знято на стенді без Starlink у ether1; потім з'явився інтернет — це не дрейф."""
    bench = ('/ip address add address=192.168.88.1/24 interface=br-lan\n/ip dhcp-client\n# Interface not active\n'
             'add comment="fleet: wan" interface=ether1\n/ip dhcp-server add interface=br-lan name=fleet-lan\n')
    field = ('/ip address add address=192.168.88.1/24 interface=br-lan\n'
             '/ip dhcp-client add comment="fleet: wan" interface=ether1\n'
             '/ip dhcp-server add interface=br-lan name=fleet-lan\n/system clock set time-zone-name=Europe/Kiev\n')
    assert drift(bench, field) == []
    # справжня зміна лишається видимою
    assert drift(bench, field.replace("ether1", "ether5")) == [
        '- /ip dhcp-client add comment="fleet: wan" interface=ether1',
        '+ /ip dhcp-client add comment="fleet: wan" interface=ether5',
    ]
    assert drift(bench, field + "/system clock set time-zone-autodetect=no time-zone-name=UTC\n") == [
        "+ /system clock set time-zone-autodetect=no time-zone-name=UTC"]


def test_every_device_reachable_through_tunnel():
    """Ззовні кожен пристрій має свою адресу в блоці /28: роутер і ФК — через netmap, камери — через dst-nat."""
    assert len(set(A.MAPPED.values())) == len(A.MAPPED)
    for host in A.HOSTS:
        assert 0 < A.MAPPED[host] < A.BLOCK_SIZE - 1
        if A.lan_ip(host) in A.LAN_MAPPED:
            assert A.MAPPED[host] == A.HOSTS[host]  # netmap зберігає номер хоста
    assert A.dstnat_hosts() == ["cam1", "cam2"]
    assert (str(A.lan_ip("cam1")), str(A.lan_ip("cam2"))) == ("192.168.88.108", "192.168.88.109")


def test_cameras_dstnat_before_netmap(provisioned, store):
    """Камери .108/.109 лежать поза блоком netmap: їхній dst-nat має спрацювати раніше за netmap."""
    robot = provisioned.robot(1)
    rsc, _ = Renderer(provisioned, store).robot_rsc(robot, provisioned.site(robot.site), robot.slot)
    nat = [line for line in rsc.splitlines() if line.startswith("/ip firewall nat add chain=dstnat")]
    assert [re.search(r"dst-address=(\S+) action=(\S+) to-addresses=(\S+)", n).groups() for n in nat] == [
        ("10.150.16.8", "dst-nat", "192.168.88.108"),
        ("10.150.16.9", "dst-nat", "192.168.88.109"),
        ("10.150.16.0/28", "netmap", "192.168.88.0/28"),
    ]
    assert "dst-address=192.168.88.108-192.168.88.109 protocol=tcp dst-port=554" in rsc  # RTSP пропускає файрвол
    assert "ranges=192.168.88.200-192.168.88.249" in rsc  # DHCP не роздає адреси камер


def test_cameras_move_from_old_addresses(provisioned, store, monkeypatch):
    """Камери, які вже стояли на .8/.9, переїжджають на .108/.109; передню й задню розрізняємо за портом."""
    import fleet.cameras as C

    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    ports = {"cam1": "ether2", "cam2": "ether3"}
    at = {"cam1": "192.168.88.9", "cam2": "192.168.88.8"}  # навмисно навхрест: адреса нічого не каже про камеру
    disabled = set()

    def live(ip):
        return next((c for c, addr in at.items() if addr == ip and ports[c] not in disabled), None)

    class FakeRouter:
        def run(self, command, check=True):
            m = re.match(r"/interface (disable|enable) (\S+)$", command)
            if m:
                (disabled.add if m[1] == "disable" else disabled.discard)(m[2])
            return ""

    class FakeCamera:
        def __init__(self, ip, user, password):
            self.ip = ip

        def ping(self):
            return live(self.ip) is not None

        def set_address(self, ip, mask, gateway):
            at[live(self.ip)] = ip

    monkeypatch.setitem(C.VENDORS, "hikvision", FakeCamera)
    C.setup_cameras(provisioned, store, provisioned.robot(1), FakeRouter(), log=lambda *_: None)
    assert at == {"cam1": "192.168.88.108", "cam2": "192.168.88.109"}
    assert not disabled  # усі порти знову ввімкнено


def test_starlink_side_only_ssh_and_discovery(provisioned, store):
    """З боку Starlink роутер видно (MNDP) і доступний лише ssh, лише з приватних адрес; для LTE — нічого."""
    robot = provisioned.robot(1)
    rsc, _ = Renderer(provisioned, store).robot_rsc(robot, provisioned.site(robot.site), robot.slot)
    wan = [line for line in rsc.splitlines() if "in-interface-list=fleet-wan" in line]
    assert [re.search(r"protocol=(\w+) dst-port=(\d+)", line).groups() for line in wan] == [("tcp", "22"), ("udp", "5678")]
    assert all("src-address-list=fleet-wan-local" in line for line in wan)
    assert "discover-interface-list=fleet-discover" in rsc and "8291" not in "".join(wan)

    provisioned.platforms["flu-v1"]["wan"] = {"type": "lte", "interface": "lte1", "apn": "internet"}
    rsc, _ = Renderer(provisioned, store).robot_rsc(robot, provisioned.site(robot.site), robot.slot)
    assert "fleet-wan" not in rsc and "discover-interface-list=fleet-lan" in rsc


def test_devices_answer_through_router(provisioned, store):
    """Камера чи адаптер ФК без шлюзу 192.168.88.1 однаково відповідають: роутер підставляє себе відправником."""
    robot = provisioned.robot(1)
    rsc, _ = Renderer(provisioned, store).robot_rsc(robot, provisioned.site(robot.site), robot.slot)
    assert ("/ip firewall nat add chain=srcnat action=masquerade out-interface=br-lan "
            "src-address-list=fleet-sources") in rsc
    assert "dst-address=192.168.88.0/24 protocol=icmp" in rsc  # ping пристроїв через тунель
