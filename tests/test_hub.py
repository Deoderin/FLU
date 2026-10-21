"""Сервер «hub» з однієї команди: команда, скрипт на «сервері» (з заглушками), відповідь, інвентар."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fleet import hub
from fleet.hub import HubError, apply_response, parse_response, prepare
from fleet.inventory import Inventory
from fleet.keystore import wg_genkey, wg_pubkey

from .conftest import REPO


@pytest.fixture
def fresh(tmp_path):
    """Копія репозиторію з його справжнім (порожнім) інвентарем — як на новому ноутбуці."""
    for name in ("inventory", "base", "templates", "server"):
        shutil.copytree(REPO / name, tmp_path / "repo" / name)
    return tmp_path / "repo"


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

    def run(self, command: str) -> str:
        env = {**os.environ, "PATH": str(self.stub) + os.pathsep + os.environ["PATH"], "FLU_TEST": "1",
               "FLU_WG_DIR": self.dir.as_posix(), "FLU_ENDPOINT_HOST": "198.51.100.20"}
        r = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
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
    assert "dst-address=192.168.88.8-192.168.88.9" in rsc  # камери .8 і .9
    assert "mtu=1280" in rsc and "MTU = 1280" in pilot_conf

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
