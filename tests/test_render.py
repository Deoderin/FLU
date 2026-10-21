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


def test_lan_addresses_fit_mapped_block():
    for host in A.HOSTS:
        assert A.lan_ip(host) in A.LAN_MAPPED
