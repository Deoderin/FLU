"""Новий сервер в AWS: команда для CloudShell, скрипт із заглушкою aws і кнопка в майстрі."""
import json
import os
import subprocess

import pytest

import fleet.aws
from fleet.aws import AwsError, server_command

from .conftest import REPO
from .test_hub import BASH, needs_bash
from .test_ui import call, simple_ui  # noqa: F401 — фікстура майстра на «новому ноутбуці»

# Заглушка AWS CLI: записує кожен виклик у $AWS_LOG і відповідає, як справжній aws --output text.
# TAKEN=1 — у регіоні вже є ключ із такою назвою (лишився від минулої спроби)
AWS_STUB = """#!/usr/bin/env bash
echo "$*" >> "$AWS_LOG"
case "$1 $2" in
  "ec2 describe-key-pairs") [ -n "$TAKEN" ] && exit 0 || exit 254 ;;
  "ec2 describe-security-groups") echo 0 ;;
  "ec2 create-key-pair") printf -- '-----BEGIN RSA PRIVATE KEY-----\\nTEST\\n-----END RSA PRIVATE KEY-----\\n' ;;
  "ec2 describe-vpcs") echo vpc-1 ;;
  "ec2 create-security-group") echo sg-1 ;;
  "ec2 describe-managed-prefix-lists") echo pl-1 ;;
  "iam get-instance-profile"|"iam get-role") exit 254 ;;
  "ssm get-parameter") echo ami-1 ;;
  "ec2 run-instances") echo i-1 ;;
  "ec2 allocate-address") echo eipalloc-1 ;;
  "ec2 describe-addresses") echo 203.0.113.7 ;;
esac
"""


def cloudshell(tmp_path, command: str, taken: bool = False) -> tuple[subprocess.CompletedProcess, str]:
    """Запустити команду, як у CloudShell: справжній bash, aws і sleep — заглушки."""
    stub, home = tmp_path / "stub", tmp_path / "home"
    stub.mkdir()
    home.mkdir()
    (stub / "aws").write_bytes(AWS_STUB.encode())
    (stub / "sleep").write_bytes(b"#!/usr/bin/env bash\n")  # роль «з'являється» одразу
    log = tmp_path / "aws.log"
    env = {**os.environ, "PATH": str(stub) + os.pathsep + os.environ["PATH"], "HOME": home.as_posix(),
           "AWS_LOG": log.as_posix(), "TAKEN": "1" if taken else ""}
    r = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, encoding="utf-8")
    return r, log.read_text("utf-8") if log.exists() else ""


def test_bad_input_rejected():
    with pytest.raises(AwsError, match="латинські"):
        server_command(REPO, "Сервер 1")
    with pytest.raises(AwsError, match="регіон"):
        server_command(REPO, "NRK-Server-1", "Stockholm")
    with pytest.raises(AwsError, match="не публічна"):
        server_command(REPO, "NRK-Server-1", "eu-north-1", "192.168.1.5")  # SSH з приватної адреси AWS не побачить


def test_command_is_one_line():
    cmd = server_command(REPO, "NRK-Server-1", "eu-north-1", "93.184.216.34")
    assert "\n" not in cmd and len(cmd) < 8000  # одним рядком: CloudShell не перепитує про багаторядкову вставку
    assert cmd.endswith("bash /tmp/create-server.sh 'NRK-Server-1' 'eu-north-1' '93.184.216.34'")


@needs_bash
def test_server_is_created(tmp_path):
    r, log = cloudshell(tmp_path, server_command(REPO, "NRK-Server-1", "eu-north-1", "93.184.216.34"))
    assert r.returncode == 0, r.stderr
    assert "ГОТОВО: сервер NRK-Server-1 (i-1) у eu-north-1, постійна адреса 203.0.113.7" in r.stdout
    assert "/home/NRK-Server-1-key.pem" in r.stdout.replace("\\", "/")
    assert (tmp_path / "home" / "NRK-Server-1-key.pem").read_text().startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert "--protocol udp --port 51820 --cidr 0.0.0.0/0" in log  # WireGuard
    assert "PrefixListId=pl-1" in log                              # SSH для кнопки Connect
    assert "--protocol tcp --port 22 --cidr 93.184.216.34/32" in log  # SSH з ноутбука
    assert "--cidr 0.0.0.0/0" not in log.replace("--port 51820 --cidr 0.0.0.0/0", "")  # SSH для всіх не відкриваємо
    assert "VolumeSize=8,VolumeType=gp3,Encrypted=true" in log


@needs_bash
def test_taken_name_creates_nothing(tmp_path):
    r, log = cloudshell(tmp_path, server_command(REPO, "NRK-Server-1", "eu-north-1", ""), taken=True)
    assert r.returncode != 0 and "назва NRK-Server-1 у регіоні eu-north-1 уже зайнята" in r.stderr
    assert "create-" not in log and "run-instances" not in log


def test_wizard_button(simple_ui, monkeypatch):
    monkeypatch.setattr(fleet.aws, "public_ip", lambda timeout=5: "93.184.216.34")
    status, body = call(simple_ui, "/api/simple/aws-command", {"name": "NRK-Server-2", "region": "eu-north-1", "ssh_here": True})
    res = json.loads(body)
    assert status == 200 and res["my_ip"] == "93.184.216.34" and not res["ip_unknown"]
    assert res["command"].endswith("'NRK-Server-2' 'eu-north-1' '93.184.216.34'")
    # адресу ноутбука дізнатися не вдалося — команда однаково є, SSH лише через Connect
    monkeypatch.setattr(fleet.aws, "public_ip", lambda timeout=5: None)
    res = json.loads(call(simple_ui, "/api/simple/aws-command", {"name": "NRK-Server-2", "region": "eu-north-1", "ssh_here": True})[1])
    assert res["ip_unknown"] and res["command"].endswith("'NRK-Server-2' 'eu-north-1' ''")
    status, body = call(simple_ui, "/api/simple/aws-command", {"name": "x; rm -rf ~", "region": "eu-north-1"})
    assert status == 400 and "латинські" in json.loads(body)["error"]
