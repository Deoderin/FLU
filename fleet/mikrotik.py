"""MikroTik по SSH: застосування конфігу на стенді, віддалений доступ, відбиток і дрейф."""
from __future__ import annotations

import logging
import re
import socket
import time
from pathlib import Path

import paramiko

from . import addressing as A
from .i18n import T
from .inventory import Inventory, Robot, Site
from .keystore import SecretStore

BENCH_HOST = str(A.lan_ip("router"))
ERROR_RE = re.compile(
    r"failure:|syntax error|bad command name|expected end of command|no such item|invalid value|input does not match",
    re.IGNORECASE,
)
# Рядки /export, що змінюються самі собою (дата, модель, серійний номер)
VOLATILE_RE = re.compile(r"^# (?:\S+ \S+ by RouterOS|software id|model|serial number)")
# Налаштування, які RouterOS міняє сам: часовий пояс визначається, щойно з'являється інтернет
SELF_SET_RE = re.compile(r"^/system clock set time-zone-name=\S+$")


# Поки роутер перезавантажується, paramiko друкує трасування обірваних з'єднань. Помилки ми обробляємо самі
logging.getLogger("paramiko").addHandler(logging.NullHandler())


class MikroTikError(Exception):
    pass


def parse_ros_duration(text: str) -> int | None:
    """Тривалість RouterOS ('00:01:05', '1d02:03:04', '1m5s', '35s') у секундах."""
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
    """SSH-сесія до RouterOS. jump — SSH-клієнт сервера майданчика, якщо йдемо через нього."""

    def __init__(self, host: str, password: str, user: str = "admin",
                 jump: paramiko.SSHClient | None = None, timeout: float = 15):
        self.host = host
        self.jump = jump
        self.client = paramiko.SSHClient()
        # Роутери перепрошиваються, і ключ хоста змінюється. Ходимо до них лише зі стенда
        # або всередині своїх тунелів WireGuard, тому ключ хоста не закріплюємо.
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
            raise MikroTikError(T(f"немає SSH до MikroTik {host}: {exc}", f"no SSH to MikroTik {host}: {exc}")) from None

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
            raise MikroTikError(T(f"{self.host}: `{command}` завершилася помилкою:\n{out.strip()}",
                                  f"{self.host}: `{command}` failed:\n{out.strip()}"))
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
            # У фоні імпорт доживе, навіть якщо ця SSH-сесія йде через тунель, що видаляється
            self.run(f':execute script="/import file-name={local.name}"')
            return
        out = self.run(f"/import file-name={local.name}")
        if "successfully" not in out:
            raise MikroTikError(T(f"{self.host}: імпорт {local.name} не завершився:\n{out.strip()}",
                                  f"{self.host}: import of {local.name} did not finish:\n{out.strip()}"))

    def fingerprint(self) -> str | None:
        comment = self.get("/system script get [find name=fleet-version] comment")
        return comment.removeprefix("fleet ").strip() if comment.startswith("fleet ") else None

    def last_handshake(self, tag: str) -> int | None:
        """Скільки секунд тому був handshake у піра майданчика (None — жодного разу)."""
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
        raise MikroTikError(T(
            f"немає SSH до сервера {site.host}: {exc}. Ключ хоста береться з ~/.ssh/known_hosts: "
            f"підключіться до сервера один раз звичайним ssh",
            f"no SSH to server {site.host}: {exc}. The host key is taken from ~/.ssh/known_hosts: "
            f"connect to the server once with plain ssh",
        )) from None
    return jump


def connect_robot(inv: Inventory, store: SecretStore, robot: Robot, via: str = "auto",
                  through: tuple[Site, int] | None = None) -> MikroTik:
    """Підключитися до MikroTik НРК.

    via: bench — зі стенда (192.168.88.1); svc — сервісним тунелем;
         tunnel — напряму через тунель пілота (hub);
         site — через сервер managed-майданчика (through = майданчик і slot, типово поточні);
         auto — svc, якщо є, інакше tunnel (hub) або site (managed).
    """
    password = store.robot(robot.id)["mikrotik_password"]
    if through is None and robot.provisioned:
        through = (inv.site(robot.site), robot.slot)
    if via == "auto":
        if robot.service_tunnel and inv.service:
            via = "svc"
        elif through and through[0].hub:
            via = "tunnel"
        elif through and through[0].managed and through[0].host:
            via = "site"
        else:
            raise MikroTikError(T(
                f"{robot.name}: немає віддаленого доступу — у НРК немає сервісного тунелю, "
                f"а майданчик не наш. Працюйте на стенді (--via bench)",
                f"{robot.name}: no remote access - the UGV has no service tunnel "
                f"and the site is not ours. Work at the bench (--via bench)",
            ))
    if via == "bench":
        return MikroTik(BENCH_HOST, password)
    if via == "svc":
        if not (robot.service_tunnel and inv.service):
            raise MikroTikError(T(f"{robot.name}: сервісний тунель вимкнено", f"{robot.name}: the service tunnel is off"))
        return MikroTik(str(A.service_robot_ip(inv.service, robot.id)), password)
    if via == "tunnel":
        # hub: ноутбук пілота в тунелі бачить роутер НРК за адресою з його блоку (netmap -> 192.168.88.1)
        if not through or not through[0].hub:
            raise MikroTikError(T(f"{robot.name}: напряму через тунель можна лише на hub-майданчику",
                                  f"{robot.name}: direct tunnel access works only on a hub site"))
        site, slot = through
        return MikroTik(str(A.mapped_ip(site, slot, "router")), password)
    if via == "site":
        if not through or not through[0].managed or not through[0].host:
            raise MikroTikError(T(f"{robot.name}: через майданчик можна йти, лише якщо він managed і має host",
                                  f"{robot.name}: going through a site needs a managed site with a host"))
        site, slot = through
        return MikroTik(str(A.robot_tunnel_ip(site, slot)), password, jump=open_jump(site))
    raise MikroTikError(T(f"невідомий спосіб підключення: {via}", f"unknown connection method: {via}"))


def find_bench_router(log=print, timeout: float = 3.0, name: str | None = None) -> str:
    """Адреса роутера на стенді: шукаємо його самі (MNDP), інакше — заводська 192.168.88.1.

    Спершу перевіряємо кабель: без адреси 192.168.88.x на Ethernet до скинутого роутера
    не дістатися, і краще одразу сказати чому, ніж чекати тайм-ауту SSH.
    name — ім'я НРК: уже прошитий роутер видно й без кабелю, якщо ноутбук у тому самому Starlink.
    """
    from . import netcheck
    from .mndp import describe, discover

    routers = discover(timeout)
    for r in routers:
        log(T(f"  знайдено роутер: {describe(r)}", f"  found router: {describe(r)}"))
    ours = [r for r in routers if name and r.get("identity") == name and r["ipv4"]]
    if ours:
        if not ours[0]["ipv4"].startswith("192.168.88."):
            log(T(f"  {name} знайдено через спільну мережу ({ours[0]['ipv4']}): кабель не потрібен",
                  f"  {name} found over the shared network ({ours[0]['ipv4']}): no cable needed"))
        return ours[0]["ipv4"]
    # Про кабель кажемо, лише коли без нього не обійтися: через спільну мережу НРК не знайшовся
    link = netcheck.check_router_link()
    log(T(f"  кабель: {link['message']}", f"  cable: {link['message']}"))
    link_ok = link["status"] in ("ok", "unknown")
    factory = [r for r in routers if r["ipv4"] == BENCH_HOST]
    if len(factory) == 1:
        if not link_ok:
            raise MikroTikError(link["message"])
        return BENCH_HOST
    if len(routers) == 1:
        if not routers[0]["ipv4"]:
            raise MikroTikError(T(
                "роутер знайдено, але в нього немає IP-адреси. Скиньте його до заводських налаштувань: затисніть "
                "кнопку Reset, увімкніть живлення й тримайте, доки не заблимає індикатор, тоді відпустіть",
                "the router is found but has no IP address. Reset it to factory settings: hold the Reset button, "
                "power it on and keep holding until the LED starts blinking, then release",
            ))
        return routers[0]["ipv4"]
    if len(routers) > 1:
        raise MikroTikError(T(
            "у мережі кілька роутерів MikroTik. Вимкніть Wi-Fi на ноутбуці й лишіть під'єднаним лише роутер НРК",
            "there are several MikroTik routers on the network. Turn off Wi-Fi on the laptop and keep only the UGV router connected",
        ))
    if not link_ok:
        raise MikroTikError(link["message"])
    log(T(f"  роутер не знайдено автоматично — пробую заводську адресу {BENCH_HOST}",
          f"  router not found automatically - trying the factory address {BENCH_HOST}"))
    return BENCH_HOST


def wait_for_file(mt: MikroTik, name: str, timeout: float = 20) -> None:
    """Файл, залитий по SFTP, з'являється в /file не одразу, а run-after-reset приймає лише файли з цього списку."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mt.get(f'/file find name="{name}"'):
            return
        time.sleep(1)
    files = mt.run("/file print terse", check=False).strip()
    raise MikroTikError(T(f"{mt.host}: залитий файл {name} не з'явився в /file за {int(timeout)} с. Файли на роутері:\n{files}",
                          f"{mt.host}: the uploaded file {name} did not appear in /file within {int(timeout)} s. Files on the router:\n{files}"))


def open_router(host: str, passwords: list[str]) -> MikroTik:
    """Вхід першим паролем, що підійде: заводським з наліпки або нашим, якщо роутер уже прошито."""
    last: MikroTikError | None = None
    for password in dict.fromkeys(passwords):
        try:
            return MikroTik(host, password)
        except MikroTikError as exc:
            last = exc
    raise last or MikroTikError(T("немає пароля для входу", "no password to log in with"))


def apply_full_config(mt: MikroTik, rsc: Path) -> None:
    """Скидання роутера з нашим конфігом замість заводського. Роутер іде в перезавантаження."""
    remote = ("flash/" if mt.has_flash() else "") + "fleet-robot.rsc"
    mt.upload(rsc, remote)
    wait_for_file(mt, remote)
    try:
        mt.run(
            f"/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset={remote}",
            timeout=15, stdin_text="y\n",
        )
    except (socket.timeout, paramiko.SSHException, EOFError, OSError):
        pass  # з'єднання рветься через перезавантаження — це нормально
    except MikroTikError as exc:
        files = mt.run("/file print terse", check=False).strip()
        raise MikroTikError(T(f"{exc}\nФайли на роутері:\n{files}", f"{exc}\nFiles on the router:\n{files}")) from None


def wait_for(host: str, password: str, timeout: float = 300) -> MikroTik:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return MikroTik(host, password, timeout=5)
        except MikroTikError as exc:
            last = exc
            time.sleep(5)
    raise MikroTikError(T(f"MikroTik {host} не повернувся за {int(timeout)} с: {last}",
                          f"MikroTik {host} did not come back within {int(timeout)} s: {last}"))


def save_baseline(robot_dir: Path, mt: MikroTik) -> Path:
    """Еталонний /export одразу після прошивки: з ним приймальний тест порівнює роутер."""
    path = robot_dir / "baseline.rsc"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(mt.export())
    return path


def export_lines(text: str) -> list[str]:
    """/export terse для порівняння: без коментарів, кожна команда — одним рядком.

    Коментар стану («# Interface not active», поки в ether1 немає Starlink) розриває рядок
    на «/ip dhcp-client» і «add ...»: склеюємо назад, щоб стан інтерфейсу не рахувався дрейфом.
    """
    lines: list[str] = []
    menu = ""
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or SELF_SET_RE.match(line):
            continue
        if line.startswith("/"):
            menu = line
            lines.append(line)
            continue
        if menu and lines and lines[-1] == menu:
            lines.pop()
        lines.append(f"{menu} {line}" if menu else line)
    return lines


def drift(baseline: str, current: str) -> list[str]:
    """Рядки, що з'явилися або зникли порівняно з еталоном."""
    old, new = set(export_lines(baseline)), set(export_lines(current))
    return [f"- {line}" for line in sorted(old - new)] + [f"+ {line}" for line in sorted(new - old)]
