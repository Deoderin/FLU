"""MikroTik по SSH: применение конфига на стенде, удалённый доступ, отпечаток и дрейф."""
from __future__ import annotations

import re
import socket
import time
from pathlib import Path

import paramiko

from . import addressing as A
from .inventory import Inventory, Robot, Site
from .keystore import SecretStore

BENCH_HOST = str(A.lan_ip("router"))
ERROR_RE = re.compile(
    r"failure:|syntax error|bad command name|expected end of command|no such item|invalid value|input does not match",
    re.IGNORECASE,
)
# Строки /export, которые меняются сами по себе (дата, модель, серийник)
VOLATILE_RE = re.compile(r"^# (?:\S+ \S+ by RouterOS|software id|model|serial number)")


class MikroTikError(Exception):
    pass


def parse_ros_duration(text: str) -> int | None:
    """Длительность RouterOS ('00:01:05', '1d02:03:04', '1m5s', '35s') в секундах."""
    text = text.strip()
    m = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(.*)", text)
    if not text or not m:
        return None
    weeks, days, rest = m.groups()
    total = int(weeks or 0) * 604800 + int(days or 0) * 86400
    try:
        if ":" in rest:
            h, mi, s = rest.split(":")
            total += int(h) * 3600 + int(mi) * 60 + float(s)
        elif rest:
            parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", rest)
            if not parts:
                return None
            total += sum(float(v) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[u] for v, u in parts)
    except ValueError:
        return None
    return int(total)


class MikroTik:
    """SSH-сессия к RouterOS. jump — SSH-клиент сервера площадки, если идём через него."""

    def __init__(self, host: str, password: str, user: str = "admin",
                 jump: paramiko.SSHClient | None = None, timeout: float = 15):
        self.host = host
        self.jump = jump
        self.client = paramiko.SSHClient()
        # Роутеры перепрошиваются, и ключ хоста меняется. Ходим к ним только со стенда
        # или внутри своих туннелей WireGuard, поэтому ключ хоста не закрепляем.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            sock = None
            if jump is not None:
                sock = jump.get_transport().open_channel("direct-tcpip", (host, 22), ("127.0.0.1", 0))
            self.client.connect(
                host, username=user, password=password, sock=sock, timeout=timeout,
                auth_timeout=timeout, banner_timeout=timeout, look_for_keys=False, allow_agent=False,
            )
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.close()
            raise MikroTikError(f"нет SSH до MikroTik {host}: {exc}") from None

    def close(self) -> None:
        self.client.close()
        if self.jump is not None:
            self.jump.close()

    def __enter__(self) -> MikroTik:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, command: str, check: bool = True, timeout: float = 60, stdin_text: str | None = None) -> str:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        if stdin_text:
            stdin.write(stdin_text)
            stdin.flush()
        out = (stdout.read() + stderr.read()).decode(errors="replace").replace("\r", "")
        if check and ERROR_RE.search(out):
            raise MikroTikError(f"{self.host}: `{command}` завершилась ошибкой:\n{out.strip()}")
        return out

    def get(self, expression: str) -> str:
        return self.run(f":put [{expression}]", check=False).strip()

    def upload(self, local: Path, remote: str) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()

    def has_flash(self) -> bool:
        return self.get('/file find name="flash"') != ""

    def import_file(self, local: Path, background: bool = False) -> None:
        self.upload(local, local.name)
        if background:
            # В фоне импорт доживёт, даже если эта SSH-сессия идёт через удаляемый туннель
            self.run(f':execute script="/import file-name={local.name}"')
            return
        out = self.run(f"/import file-name={local.name}")
        if "successfully" not in out:
            raise MikroTikError(f"{self.host}: импорт {local.name} не завершился:\n{out.strip()}")

    def fingerprint(self) -> str | None:
        comment = self.get("/system script get [find name=fleet-version] comment")
        return comment.removeprefix("fleet ").strip() if comment.startswith("fleet ") else None

    def last_handshake(self, tag: str) -> int | None:
        """Сколько секунд назад был handshake у пира площадки (None — ни разу)."""
        return parse_ros_duration(self.get(f'/interface wireguard peers get [find comment="{tag}"] last-handshake'))

    def export(self) -> str:
        out = self.run("/export terse")
        return "\n".join(line for line in out.splitlines() if not VOLATILE_RE.match(line)).strip() + "\n"


def open_jump(site: Site) -> paramiko.SSHClient:
    jump = paramiko.SSHClient()
    jump.load_system_host_keys()
    jump.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        jump.connect(site.host, username=site.ssh_user, timeout=15)
    except (paramiko.SSHException, OSError) as exc:
        raise MikroTikError(
            f"нет SSH до сервера {site.host}: {exc}. Ключ хоста берётся из ~/.ssh/known_hosts: "
            f"подключитесь к серверу один раз обычным ssh"
        ) from None
    return jump


def connect_robot(inv: Inventory, store: SecretStore, robot: Robot, via: str = "auto",
                  through: tuple[Site, int] | None = None) -> MikroTik:
    """Подключиться к MikroTik робота.

    via: bench — со стенда (192.168.88.1); svc — по сервисному туннелю;
         site — через сервер managed-площадки (through = площадка и slot, по умолчанию текущие);
         auto — svc, если есть, иначе site.
    """
    password = store.robot(robot.id)["mikrotik_password"]
    if through is None and robot.provisioned:
        through = (inv.site(robot.site), robot.slot)
    if via == "auto":
        if robot.service_tunnel and inv.service:
            via = "svc"
        elif through and through[0].managed and through[0].host:
            via = "site"
        else:
            raise MikroTikError(
                f"{robot.name}: нет удалённого доступа — у робота нет сервисного туннеля, "
                f"а площадка не наша. Работайте на стенде (--via bench)"
            )
    if via == "bench":
        return MikroTik(BENCH_HOST, password)
    if via == "svc":
        if not (robot.service_tunnel and inv.service):
            raise MikroTikError(f"{robot.name}: сервисный туннель выключен")
        return MikroTik(str(A.service_robot_ip(inv.service, robot.id)), password)
    if via == "site":
        if not through or not through[0].managed or not through[0].host:
            raise MikroTikError(f"{robot.name}: через площадку можно идти только если она managed и у неё задан host")
        site, slot = through
        return MikroTik(str(A.robot_tunnel_ip(site, slot)), password, jump=open_jump(site))
    raise MikroTikError(f"неизвестный способ подключения: {via}")


def apply_full_config(mt: MikroTik, rsc: Path) -> None:
    """Сброс роутера с нашим конфигом вместо заводского. Роутер уходит в перезагрузку."""
    remote = ("flash/" if mt.has_flash() else "") + "fleet-robot.rsc"
    mt.upload(rsc, remote)
    try:
        mt.run(
            f"/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset={remote}",
            timeout=15, stdin_text="y\n",
        )
    except (socket.timeout, paramiko.SSHException, EOFError, OSError):
        pass  # соединение рвётся из-за перезагрузки — это нормально


def wait_for(host: str, password: str, timeout: float = 300) -> MikroTik:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return MikroTik(host, password, timeout=5)
        except MikroTikError as exc:
            last = exc
            time.sleep(5)
    raise MikroTikError(f"MikroTik {host} не вернулся за {int(timeout)} с: {last}")


def save_baseline(robot_dir: Path, mt: MikroTik) -> Path:
    """Эталонный /export сразу после прошивки: с ним приёмочный тест сравнивает роутер."""
    path = robot_dir / "baseline.rsc"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(mt.export())
    return path


def drift(baseline: str, current: str) -> list[str]:
    """Строки, которые появились или пропали по сравнению с эталоном."""
    old, new = set(baseline.splitlines()), set(current.splitlines())
    return [f"- {line}" for line in sorted(old - new)] + [f"+ {line}" for line in sorted(new - old)]
