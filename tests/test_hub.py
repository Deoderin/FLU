"""Сервер «hub» з однієї команди: команда, скрипт на «сервері» (з заглушками), відповідь, інвентар."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fleet import hub
from fleet.hub import HubError, apply_response, parse_response, prepare
from fleet.inventory import Inventory
from fleet.keystore import SecretStore, wg_genkey, wg_pubkey
from fleet.render import Renderer

from .conftest import REPO


def laptop(path: Path) -> Path:
    """Копія репозиторію, як на новому ноутбуці: лише шаблони інвентарю, inventory/ програма створить сама."""
    for name in ("inventory.example", "base", "templates", "server"):
        shutil.copytree(REPO / name, path / name)
    return path


@pytest.fixture
def fresh(tmp_path):
    return laptop(tmp_path / "repo")


@pytest.fixture
def other(tmp_path):
    """Другий ноутбук зі своїми ключами, той самий сервер."""
    return laptop(tmp_path / "other")


def find_bash() -> str | None:
    if os.name == "nt":  # bash.exe із System32 — це WSL, нам потрібен Git Bash
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files\Git"):
            for cand in (Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "bin" / "bash.exe"):
                if cand.exists():
                    return str(cand)
        return None
    return shutil.which("bash")


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="немає bash (Git Bash на Windows)")


class FakeServer:
    """Каталог «сервера» і заглушка wg: справжні ключі, без системних змін (FLU_TEST=1)."""

    def __init__(self, tmp: Path, used_ports: str = ""):
        self.dir = tmp / "wg"
        self.stub = tmp / "stub"
        self.stub.mkdir(parents=True)
        key = wg_genkey()
        self.pubkey = wg_pubkey(key)
        (self.stub / "wg").write_bytes((
            "#!/usr/bin/env bash\n"
            f"case \"$1\" in\n  genkey) echo '{key}' ;;\n  pubkey) cat >/dev/null; echo '{self.pubkey}' ;;\n"
            f"  show) printf '{used_ports}' ;;\nesac\n").encode())

    def run(self, command: str, ok: bool = True) -> str:
        env = {**os.environ, "PATH": str(self.stub) + os.pathsep + os.environ["PATH"], "FLU_TEST": "1",
               "FLU_WG_DIR": self.dir.as_posix(), "FLU_ENDPOINT_HOST": "198.51.100.20"}
        r = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, encoding="utf-8")
        assert (r.returncode == 0) == ok, r.stderr
        return r.stdout + r.stderr

    def peers(self) -> list[list[str]]:
        return [line.split("\t") for line in (self.dir / "flu-peers.tsv").read_text().splitlines()]


def test_prepare_creates_keys_and_ugv(fresh):
    res = prepare(fresh, "ivan", "FLU-2026-0001")
    inv = Inventory.load(fresh)
    assert (res["robot_id"], inv.robot(1).serial, inv.robot(1).slot) == (1, "FLU-2026-0001", None)
    assert (fresh / "secrets" / "operators" / "ivan.json").exists()
    assert res["command"].startswith("echo H4sI") and "'ivan'" in res["command"] and "'FLU-2026-0001'" in res["command"]
    # повторно — той самий НРК і ті самі ключі
    assert prepare(fresh, "ivan", "FLU-2026-0001")["command"] == res["command"]


def test_bad_input_rejected(fresh):
    with pytest.raises(HubError, match="ім'я пілота"):
        prepare(fresh, "Іван", "FLU-1")
    with pytest.raises(HubError, match="номер НРК"):
        prepare(fresh, "ivan", "FLU 1; rm -rf /")
    with pytest.raises(HubError, match=r"кирилиця \(ПФ\)"):
        prepare(fresh, "ivan", "ПФ-01")


@needs_bash
def test_round_trip(fresh, tmp_path):
    server = FakeServer(tmp_path)
    cmd = prepare(fresh, "ivan", "FLU-2026-0001")["command"]
    out = server.run(cmd)
    assert "WireGuard уже встановлено" in out

    # вставка «з термінала»: переноси рядків посередині й зайвий текст довкола
    line = next(x for x in out.splitlines() if x.startswith("FLU1:"))
    pasted = "root@vps:~# ...\n" + "\n".join(line[i:i + 70] for i in range(0, len(line), 70)) + "\nroot@vps:~#"
    summary = apply_response(fresh, pasted)
    assert summary == {"endpoint": "198.51.100.20:51820", "pilot": "ivan", "pilot_ip": "10.150.1.10",
                       "robot": "nrk-001", "robot_id": 1, "slot": 1, "qgc": "10.150.16.2:5760"}

    inv = Inventory.load(fresh)
    site = inv.site("main")
    assert (site.kind, site.server_pubkey, str(site.endpoint)) == ("hub", server.pubkey, "198.51.100.20:51820")
    assert inv.validate() == []
    pilot_conf = (fresh / "build" / "sites" / "main" / "operators" / "ivan.conf").read_text("utf-8")
    assert "AllowedIPs = 10.150.0.1/32, 10.150.16.0/20" in pilot_conf and "Address = 10.150.1.10/32" in pilot_conf
    rsc = (fresh / "build" / "robots" / "nrk-001" / "robot.rsc").read_text("utf-8")
    assert "allowed-address=10.150.0.0/24,10.150.1.0/24" in rsc
    assert '/ip route add dst-address=10.150.1.0/24 gateway=wg-main' in rsc
    assert 'dst-address=192.168.88.108 comment="fleet: front camera' in rsc  # передня і задня камери
    assert 'dst-address=192.168.88.109 comment="fleet: rear camera' in rsc
    assert "mtu=1280" in rsc and "MTU = 1280" in pilot_conf
    # UDP, який адаптер ФК шле сам (на старий наземний ноутбук), — на сервер, той віддасть пілоту-власнику
    assert ("chain=dstnat in-interface=br-lan src-address=192.168.88.2 protocol=udp dst-address-type=!local "
            "dst-port=!67-68 action=dst-nat to-addresses=10.150.0.1") in rsc
    assert "chain=srcnat src-address=192.168.88.2 out-interface=wg-main action=src-nat to-addresses=10.150.16.2" in rsc
    assert "out-interface-list=fleet-sites src-address=192.168.88.2 protocol=udp" in rsc  # файрвол роутера випускає
    streams = Renderer(inv, SecretStore(fresh)).camera_streams(inv.robot(1))
    assert [(c["cam"], c["ip"]) for c in streams] == [("cam1", "10.150.16.8"), ("cam2", "10.150.16.9")]
    assert streams[0]["rtsp"] == "rtsp://admin:a12345677@10.150.16.8:554/cam/realmonitor?channel=1&subtype=0"
    assert streams[0]["rtsp2"].endswith("@10.150.16.8:554/cam/realmonitor?channel=1&subtype=1")

    conf = (server.dir / "flu.conf").read_text()
    assert "AllowedIPs = 10.150.1.10/32" in conf and "AllowedIPs = 10.150.0.2/32, 10.150.16.0/28" in conf
    assert "MTU = 1280" in conf

    # той самий пілот і НРК вдруге — ті самі адреси; другий НРК — наступний слот
    assert server.run(cmd).count("слот 1") == 1
    out2 = server.run(prepare(fresh, "ivan", "FLU-2026-0002")["command"])
    apply_response(fresh, out2)
    assert Inventory.load(fresh).robot(2).slot == 2
    assert sorted(p[:3] for p in server.peers()) == [["pilot", "10", "ivan"], ["robot", "1", "FLU-2026-0001"],
                                                     ["robot", "2", "FLU-2026-0002"]]
    assert "AllowedIPs = 10.150.0.3/32, 10.150.16.16/28" in (server.dir / "flu.conf").read_text()


@needs_bash
def test_busy_port_moves_to_next(fresh, tmp_path):
    server = FakeServer(tmp_path, used_ports="wg0\\t51820\\n")
    out = server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    assert apply_response(fresh, out)["endpoint"] == "198.51.100.20:51821"


@needs_bash
def test_response_for_another_laptop_rejected(fresh, tmp_path):
    server = FakeServer(tmp_path)
    out = server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    (fresh / "secrets" / "operators" / "ivan.json").unlink()  # «інший ноутбук»: ключа пілота тут немає
    with pytest.raises(HubError, match="іншого пілота"):
        apply_response(fresh, out)


def test_damaged_response():
    with pytest.raises(HubError, match="не знайшов"):
        parse_response("щось інше")
    with pytest.raises(HubError, match="не повністю"):
        parse_response("FLU1:eyJ2IjoxLCJlbmRwb2ludCI6IjE5:END")


def test_script_has_lf_endings():
    assert b"\r\n" not in (REPO / hub.SCRIPT).read_bytes()


def acl_rules(server) -> list[str]:
    return [line for line in (server.dir / "flu-acl.sh").read_text().splitlines() if line.startswith("iptables -A flu-acl")]


@needs_bash
def test_each_pilot_sees_only_own_ugvs(fresh, tmp_path):
    server = FakeServer(tmp_path)
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    out = server.run(prepare(fresh, "petro", "FLU-2026-0002")["command"])
    assert "НРК FLU-2026-0002 бачить лише пілот petro" in out
    assert acl_rules(server) == [
        "iptables -A flu-acl -s 10.150.1.10/32 -d 10.150.16.0/28 -j ACCEPT  # ivan -> UGV FLU-2026-0001",
        "iptables -A flu-acl -s 10.150.1.11/32 -d 10.150.16.16/28 -j ACCEPT  # petro -> UGV FLU-2026-0002",
    ]
    conf = (server.dir / "flu.conf").read_text()
    assert f"PostUp = sh {(server.dir / 'flu-acl.sh').as_posix()}" in conf
    assert "10.150.16.0/20 -j ACCEPT" not in conf  # старе правило «усі пілоти до всіх НРК»

    # передача: той самий НРК з іншим пілотом
    out = server.run(prepare(fresh, "petro", "FLU-2026-0001")["command"])
    assert "НРК FLU-2026-0001 передано від пілота ivan пілоту petro" in out
    assert acl_rules(server) == [
        "iptables -A flu-acl -s 10.150.1.11/32 -d 10.150.16.0/28 -j ACCEPT  # petro -> UGV FLU-2026-0001",
        "iptables -A flu-acl -s 10.150.1.11/32 -d 10.150.16.16/28 -j ACCEPT  # petro -> UGV FLU-2026-0002",
    ]


@needs_bash
def test_ugv_without_owner_is_hidden(fresh, tmp_path):
    server = FakeServer(tmp_path)
    server.dir.mkdir(parents=True)
    # запис, зроблений до появи власників: чотири колонки
    (server.dir / "flu-peers.tsv").write_text(f"robot\t1\tOLD-1\t{wg_pubkey(wg_genkey())}\n")
    out = server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    assert "не бачить ніхто: OLD-1" in out
    assert acl_rules(server) == ["iptables -A flu-acl -s 10.150.1.10/32 -d 10.150.16.16/28 -j ACCEPT  # ivan -> UGV FLU-2026-0001"]


@needs_bash
def test_same_name_from_another_laptop_refused(fresh, other, tmp_path):
    """Спільний сервер: інший ноутбук з тим самим ім'ям пілота чи номером НРК не витісняє першого."""
    server = FakeServer(tmp_path)
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    db = (server.dir / "flu-peers.tsv").read_text()
    out = server.run(prepare(other, "ivan", "FLU-2026-0009")["command"], ok=False)
    assert "ім'я пілота ivan на цьому сервері вже зайняте іншим ноутбуком" in out
    out = server.run(prepare(other, "petro", "FLU-2026-0001")["command"], ok=False)
    assert "НРК з номером FLU-2026-0001 уже зареєстровано" in out
    assert (server.dir / "flu-peers.tsv").read_text() == db  # відмова нічого не змінила
    server.run(prepare(fresh, "ivan", "FLU-2026-0002")["command"])  # свій ноутбук — як і раніше


@needs_bash
def test_give_and_remove_by_name(fresh, other, tmp_path):
    """Передача НРК і службові команди: лише імена, тож працюють з будь-якого ноутбука."""
    server = FakeServer(tmp_path)
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    server.run(prepare(other, "petro", None)["command"])
    out = server.run(hub.admin_command(other, "give", serial="FLU-2026-0001", pilot="petro"))
    assert "НРК FLU-2026-0001 передано від пілота ivan пілоту petro" in out
    assert "MAVLink 10.150.16.2:5760, передня камера 10.150.16.8, задня камера 10.150.16.9" in out
    assert acl_rules(server) == ["iptables -A flu-acl -s 10.150.1.11/32 -d 10.150.16.0/28 -j ACCEPT  # petro -> UGV FLU-2026-0001"]
    assert "пілота nobody на цьому сервері немає" in server.run(
        hub.admin_command(fresh, "give", serial="FLU-2026-0001", pilot="nobody"), ok=False)

    out = server.run(hub.admin_command(fresh, "remove-pilot", pilot="petro"))
    assert "не бачить ніхто: FLU-2026-0001" in out and acl_rules(server) == []
    server.run(prepare(fresh, "petro", None)["command"])  # ім'я звільнилось — його може взяти інший ноутбук
    assert "слот 1 вільний" in server.run(hub.admin_command(fresh, "remove-robot", serial="FLU-2026-0001"))
    assert [p[:3] for p in server.peers()] == [["pilot", "10", "ivan"], ["pilot", "11", "petro"]]
    with pytest.raises(HubError, match="кирилиця"):
        hub.admin_command(fresh, "remove-robot", serial="ПФ-01")


def tel_rules(server) -> list[str]:
    return [line for line in (server.dir / "flu-acl.sh").read_text().splitlines() if "-A flu-tel" in line]


@needs_bash
def test_fc_telemetry_goes_to_owner(fresh, other, tmp_path):
    """Адаптер ФК сам шле телеметрію по UDP: роутер — на сервер, сервер — пілоту-власнику, і після передачі теж."""
    server = FakeServer(tmp_path)
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    assert tel_rules(server) == [
        "iptables -t nat -A flu-tel -s 10.150.16.0/28 -d 10.150.0.1 -p udp -j DNAT --to-destination 10.150.1.10  # UGV FLU-2026-0001 -> ivan",
        "iptables -A flu-tel -s 10.150.16.0/28 -d 10.150.1.10/32 -p udp -j ACCEPT  # UGV FLU-2026-0001 -> ivan",
    ]
    acl = (server.dir / "flu-acl.sh").read_text()
    assert "iptables -t nat -I PREROUTING -i flu -j flu-tel" in acl and "-i flu -o flu -j flu-tel" in acl
    assert "iptables -t nat -X flu-tel" in (server.dir / "flu.conf").read_text()  # прибирається під час down

    server.run(prepare(other, "petro", None)["command"])
    server.run(hub.admin_command(other, "give", serial="FLU-2026-0001", pilot="petro"))
    assert all(line.endswith("# UGV FLU-2026-0001 -> petro") and "10.150.1.11" in line for line in tel_rules(server))



def test_server_command_fits_console(fresh):
    """Без коментарів скрипт стискається в команду, яку приймає будь-яка консоль (до ~8 тис. символів)."""
    cmd = prepare(fresh, "pilot-with-long-name", "SERIAL-WITH-A-LONG-NAME-0001")["command"]
    assert len(cmd) < 7500


@needs_bash
def test_server_routes_only_networks(tmp_path):
    """Маршрути на сервері: лише мережі пірів. У ключах base64 теж буває «/» — їх не чіпаємо."""
    script = (REPO / hub.SCRIPT).read_text("utf-8")
    start = script.index("sync_routes() {")
    func = script[start:script.index("\n}\n", start) + 3]
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "wg").write_bytes(
        b"#!/usr/bin/env bash\n"
        b"printf 'kI7MS302K3AUyeHKE+tD/PElfkJy1fwflOnY0neIlDA=\t10.150.0.2/32 10.150.16.0/28\n"
        b"fpz42+vMaE8FjKLhK12db/0Z3/fJlYhwuJ5z5mgQJhM=\t10.150.1.10/32\nZYfGTswchfwpooEsJ1gIkcx45eCrP7wDhPMhhOQLLkU=\t(none)\n'\n")
    (stub / "ip").write_bytes(
        b'#!/usr/bin/env bash\nif [ "$*" = "-4 route show dev flu" ]; then\n'
        b'  printf "10.150.0.0/24 proto kernel scope link src 10.150.0.1\n10.150.1.10 scope link\n10.150.16.48/28 scope link\n"\n'
        b'else echo "ip $*" >> "$LOG"; fi\n')
    log = tmp_path / "ip.log"
    env = {**os.environ, "PATH": str(stub) + os.pathsep + os.environ["PATH"], "LOG": log.as_posix()}
    r = subprocess.run([BASH, "-c", f"set -euo pipefail; IFACE=flu\n{func}\nsync_routes"], env=env,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "ip -4 route replace 10.150.0.2/32 dev flu",
        "ip -4 route replace 10.150.1.10/32 dev flu",
        "ip -4 route replace 10.150.16.0/28 dev flu",
        "ip -4 route del 10.150.16.48/28 dev flu",  # мережа видаленого НРК
    ]


@needs_bash
def test_give_is_off_in_the_built_app(fresh, other, tmp_path, monkeypatch):
    """Передачу ще не перевірено: у зібраній програмі її немає ні в картці 7, ні через повторне додавання НРК."""
    import sys

    server = FakeServer(tmp_path)
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])
    server.run(prepare(fresh, "petro", None)["command"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("FLEET_EXPERIMENTAL", raising=False)
    with pytest.raises(HubError, match="передача НРК у цій версії програми вимкнена"):
        hub.admin_command(fresh, "give", serial="FLU-2026-0001", pilot="petro")
    db = (server.dir / "flu-peers.tsv").read_text()
    out = server.run(prepare(fresh, "petro", "FLU-2026-0001")["command"], ok=False)  # той самий НРК, інший пілот
    assert "уже належить пілоту ivan, а передача НРК у цій версії програми вимкнена" in out
    assert (server.dir / "flu-peers.tsv").read_text() == db  # нічого не змінилось
    server.run(prepare(fresh, "ivan", "FLU-2026-0001")["command"])  # свій НРК своєму пілоту — як і раніше
    monkeypatch.setenv("FLEET_EXPERIMENTAL", "1")  # увімкнути, щоб перевірити в зібраній
    assert "--give" in hub.admin_command(fresh, "give", serial="FLU-2026-0001", pilot="petro")
