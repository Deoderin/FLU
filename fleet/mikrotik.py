"""MikroTik по SSH: застосування конфігу на стенді, віддалений доступ, відбиток і дрейф."""
from __future__ import annotations

import ipaddress
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


class MikroTikAuthError(MikroTikError):
    """Роутер відповів, але пароль не підійшов — це не «роутер не відповідає»."""


class StarlinkOnLan(MikroTikError):
    """Starlink не в порту 1, а в порту мосту НРК (2–5): прошивати не можна — роутер лишився б без інтернету,
    а пошук пристроїв бачить там телефони з Wi-Fi Starlink і приймає їх за камери."""


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
        except paramiko.AuthenticationException as exc:
            self.close()
            raise MikroTikAuthError(T(f"MikroTik {host}: пароль не підійшов ({exc})",
                                      f"MikroTik {host}: wrong password ({exc})")) from None
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
    from .mndp import cap_mode, describe, discover

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
    if name and link["status"] in ("no_adapter", "no_cable"):
        # Без кабелю прошити можна лише наш НРК, а його в спільній мережі немає. Кажемо, що шукали
        # і чого там не видно, а не лише «немає порту» (і не «вимкніть Wi-Fi», коли роутерів там кілька)
        raise MikroTikError(T(f"{name} через Wi-Fi не знайдено. Без кабелю видно лише НРК, уже прошитий цією програмою, коли ноутбук "
                    f"у Wi-Fi того самого Starlink. Новий чи скинутий роутер — тільки кабелем: {link['message']}",
                    f"{name} was not found over Wi-Fi. Without a cable only a UGV already flashed by this program is visible, "
                    f"when the laptop is on the Wi-Fi of the same Starlink. A new or reset router needs a cable: {link['message']}"))
    factory = [r for r in routers if r["ipv4"] == BENCH_HOST]
    if len(factory) == 1:
        if not link_ok:
            raise MikroTikError(link["message"])
        return BENCH_HOST
    if len(routers) == 1:
        if not routers[0]["ipv4"] and cap_mode(routers[0]):
            raise MikroTikError(T(
                "роутер у режимі CAP (кнопку Reset тримали задовго) і без IP-адреси. Скиньте ще раз: затисніть Reset, "
                "увімкніть живлення й відпустіть, щойно індикатор почне блимати (≈5 с), не чекаючи рівного світла. "
                "Або вставте в порт 1 кабель від мережі з інтернетом — роутер отримає адресу, і програма прошиє його й так",
                "the router is in CAP mode (the Reset button was held too long) and has no IP address. Reset it again: hold "
                "Reset, power it on and release as soon as the LED starts blinking (~5 s), without waiting for a solid light. "
                "Or plug a cable from a network with internet into port 1 - the router gets an address and can be flashed anyway",
            ))
        if not routers[0]["ipv4"]:
            raise MikroTikError(T(
                "роутер знайдено, але в нього немає IP-адреси. Скиньте його до заводських налаштувань: затисніть "
                "кнопку Reset, увімкніть живлення й тримайте, доки не заблимає індикатор, тоді відпустіть",
                "the router is found but has no IP address. Reset it to factory settings: hold the Reset button, "
                "power it on and keep holding until the LED starts blinking, then release",
            ))
        ip = routers[0]["ipv4"]
        # Роутер не на заводській адресі, але в мережі ноутбука: reset-configuration прошиє його й так
        if cap_mode(routers[0]):
            log(T(f"  роутер у режимі CAP: кнопку Reset тримали задовго (відпускати треба, щойно індикатор почне "
                  f"блимати, а не коли світиться рівно). Прошиваю й так — за адресою {ip}",
                  f"  the router is in CAP mode: the Reset button was held too long (release it as soon as the LED "
                  f"starts blinking, not when it is solid). Flashing it anyway at {ip}"))
        elif not ip.startswith("192.168.88."):
            log(T(f"  роутер не скинуто до заводських налаштувань — прошиваю за його адресою {ip} (потрібен його поточний пароль)",
                  f"  the router is not reset to factory settings - flashing it at its address {ip} (its current password is needed)"))
        return ip
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


def wait_for_file(mt: MikroTik, name: str, timeout: float = 20, size: int | None = None) -> None:
    """Файл, залитий по SFTP, з'являється в /file не одразу, а run-after-reset приймає лише файли з цього списку.

    size — скільки байтів має бути: скидання з недописаним конфігом лишає роутер напівпорожнім, і після нього
    «не повернувся за 300 с» без жодної підказки (поле 25.10). Чекаємо, лише поки файл менший; розмір, який
    RouterOS показує не числом, не звіряємо — краще прошити, ніж зупинити прошивку через формат.
    """
    deadline = time.monotonic() + timeout
    got = ""
    while time.monotonic() < deadline:
        if mt.get(f'/file find name="{name}"'):
            got = mt.get(f'/file get [find name="{name}"] size') if size is not None else ""
            if size is None or not got.isdigit() or int(got) >= size:
                return
        time.sleep(1)
    files = mt.run("/file print terse", check=False).strip()
    if got:
        raise MikroTikError(T(f"{mt.host}: залитий файл {name} має {got} байт замість {size} — не скидаю роутер з "
                              f"недописаним конфігом. Запустіть прошивку ще раз. Файли на роутері:\n{files}",
                              f"{mt.host}: the uploaded file {name} has {got} bytes instead of {size} - not resetting the "
                              f"router with a partial config. Flash again. Files on the router:\n{files}"))
    raise MikroTikError(T(f"{mt.host}: залитий файл {name} не з'явився в /file за {int(timeout)} с. Файли на роутері:\n{files}",
                          f"{mt.host}: the uploaded file {name} did not appear in /file within {int(timeout)} s. Files on the router:\n{files}"))


def open_router(host: str, passwords: list[str]) -> MikroTik:
    """Вхід першим паролем, що підійде: заводським з наліпки або нашим, якщо роутер уже прошито."""
    last: MikroTikError | None = None
    for password in dict.fromkeys(passwords):
        try:
            return MikroTik(host, password)
        except MikroTikAuthError as exc:
            last = exc
        except MikroTikError as exc:
            last = last if isinstance(last, MikroTikAuthError) else exc  # «пароль не той» важливіший за тайм-аут
    raise last or MikroTikError(T("немає пароля для входу", "no password to log in with"))


DEVICE_LABELS = {"cam1": ("передня камера", "front camera"), "cam2": ("задня камера", "rear camera"),
                 "fc": ("ФК", "FC")}
IP_MAC_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")
ADDRESS_RE = re.compile(r"(?<![-\w])address=([\d.]+)")
CIDR_RE = re.compile(r"(?<![-\w])address=([\d.]+/\d+)")
IFACE_RE = re.compile(r"(?<![-\w])interface=(\S+)")
MAC_FIELD_RE = re.compile(r"\bmac-address=([0-9A-Fa-f:]{17})")
NO_DEVICE_NETS = ("0.0.0.0/8", "169.254.0.0/16", "127.0.0.0/8")
PROBE_COMMENT = "fleet-probe"  # тимчасові адреси роутера на час опиту; скидання конфігу їх однаково стирає
PROBE_SECONDS = 12  # активний опит однієї мережі /24
DEVICE_WAIT = 90    # скільки чекати пристрої з лінком, що ввімкнулися разом із роутером і ще вантажаться
RESET_ATTEMPTS = 6  # скільки разів пробувати скидання, поки RouterOS не прийме щойно залитий файл (~15 с)


def parse_bridge_hosts(text: str) -> dict[str, list[str]]:
    """/interface bridge host print terse: які MAC видно за кожним портом мосту."""
    hosts: dict[str, list[str]] = {}
    for line in text.splitlines():
        mac, port = MAC_FIELD_RE.search(line), re.search(r"\bon-interface=(\S+)", line)
        if mac and port and mac[1].upper() not in hosts.setdefault(port[1], []):
            hosts[port[1]].append(mac[1].upper())
    return hosts


def parse_ip_macs(text: str) -> dict[str, list[str]]:
    """MAC -> IP: таблиця /tool ip-scan («192.168.15.108  40:7A:...») і /ip arp print terse (address=... mac-address=...)."""
    seen: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = IP_MAC_RE.search(line)
        pair = (m[1], m[2]) if m else None
        if pair is None:
            a, b = ADDRESS_RE.search(line), MAC_FIELD_RE.search(line)
            pair = (a[1], b[1]) if a and b else None
        if pair and pair[0] not in seen.setdefault(pair[1].upper(), []):
            seen[pair[1].upper()].append(pair[0])
    return seen


def pick_devices(ports: dict[str, str], hosts: dict[str, list[str]], ip_macs: dict[str, list[str]],
                 own: set[str], log=print, links: dict[str, bool] | None = None) -> dict[str, dict[str, str]]:
    """Пристрій у порту cam1 / cam2 / fc і його адреса. Камери розрізняємо за портом, а не за адресою.

    links — чи є лінк у порту (False — кабелю немає чи пристрій вимкнено), щоб сказати, чому порт порожній.
    """
    def usable(ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        return (ip not in own and not A.in_starlink_net(ip)
                and not any(addr in ipaddress.ip_network(n) for n in NO_DEVICE_NETS))

    found: dict[str, dict[str, str]] = {}
    for host in A.DEVICES:
        port, label = ports.get(host), T(*DEVICE_LABELS[host])
        macs = hosts.get(port, [])
        if not macs and (links or {}).get(host) is False:
            log(T(f"  {label} ({port}): у порту немає лінку — кабель не вставлено або пристрій без живлення",
                  f"  {label} ({port}): no link on the port - the cable is out or the device has no power"))
            continue
        if not macs:
            log(T(f"  {label} ({port}): у порту нічого не видно — лишаю адресу, що була",
                  f"  {label} ({port}): nothing on the port - keeping the previous address"))
            continue
        real = [mac for mac in macs if not A.random_mac(mac)]
        if not real:
            log(T(f"  {label} ({port}): видно лише {', '.join(macs)} — випадкові MAC телефонів чи ноутбуків, не камера "
                  f"й не ФК (так буває, коли в порт потрапила мережа Starlink) — не беру",
                  f"  {label} ({port}): only {', '.join(macs)} - random MACs of phones or laptops, not a camera or "
                  f"the FC (it happens when the Starlink network got into the port) - not using them"))
            continue
        pairs = [(mac, ip) for mac in real for ip in ip_macs.get(mac, []) if usable(ip)]
        starlink = [(mac, ip) for mac in real for ip in ip_macs.get(mac, []) if ip not in own and A.in_starlink_net(ip)]
        if not pairs and starlink:
            mac, ip = starlink[0]
            log(T(f"  {label} ({port}): {ip} ({mac}) — це мережа Starlink (192.168.1.x): роутер, підключений до неї ще й "
                  f"на мосту, губить шлюз Starlink, і НРК без зв'язку. Змініть пристрою адресу (камері — Dahua ConfigTool, "
                  f"наприклад на 192.168.15.109) і шукайте ще раз",
                  f"  {label} ({port}): {ip} ({mac}) - this is the Starlink network (192.168.1.x): a router joined to it on "
                  f"the bridge too loses the Starlink gateway, and the UGV is offline. Change the device address (a camera - "
                  f"Dahua ConfigTool, e.g. to 192.168.15.109) and search again"))
            continue
        if not pairs:
            log(T(f"  {label} ({port}): пристрій {real[0]} є, але його адресу не знайдено — лишаю адресу, що була",
                  f"  {label} ({port}): device {real[0]} is there but its address was not found - keeping the previous one"))
            continue
        # Кілька адрес — беремо стандартну, якщо вона серед них, інакше першу
        mac, ip = next((p for p in pairs if p[1] == str(A.lan_ip(host))), pairs[0])
        found[host] = {"ip": ip, "mac": mac}
        log(T(f"  {label} ({port}): {ip} ({mac})", f"  {label} ({port}): {ip} ({mac})"))
    by_ip: dict[str, list[str]] = {}
    for host, dev in found.items():
        by_ip.setdefault(dev["ip"], []).append(host)
    for ip, same in by_ip.items():
        if len(same) > 1:  # дві камери із заводською адресою: роутер не розведе їх по різних адресах тунелю
            names = ", ".join(T(*DEVICE_LABELS[h]) for h in same)
            log(T(f"  увага: {names} на одній адресі {ip} — змініть одній із них адресу (Dahua ConfigTool), "
                  f"інакше через тунель буде видно лише одну", f"  warning: {names} share the address {ip} - change "
                  f"the address of one of them (Dahua ConfigTool), otherwise only one is visible through the tunnel"))
            for h in same:
                del found[h]
    return found


def starlink_on_lan(hosts: dict[str, list[str]], ip_macs: dict[str, list[str]], own: set[str]) -> tuple[str, str] | None:
    """(порт мосту, MAC), за яким відповідає шлюз Starlink 192.168.1.1: Starlink воткнули не в порт 1.
    ARP роутера знає й Starlink на ether1, але ether1 не в мості — серед hosts його немає."""
    gw = str(A.STARLINK_GW)
    if gw in own:  # тимчасова адреса самого роутера на час опиту
        return None
    return next(((port, mac) for port, macs in hosts.items() for mac in macs if gw in ip_macs.get(mac, [])), None)


def starlink_on_lan_error(port: str, mac: str) -> StarlinkOnLan:
    n = port.removeprefix("ether")
    return StarlinkOnLan(T(
        f"у порту {n} ({port}) роутера — мережа Starlink: там відповідає роутер Starlink {A.STARLINK_GW} ({mac}). "
        f"Starlink — лише в порт 1. З ним у портах 2–5 (камери, ФК, ноутбук) НРК лишається без інтернету й тунелю, "
        f"а пошук приймає за камеру телефони з Wi-Fi Starlink. Переставте кабель Starlink у порт 1 і запустіть "
        f"прошивку ще раз. Роутер не прошито",
        f"port {n} ({port}) of the router has the Starlink network: the Starlink router {A.STARLINK_GW} ({mac}) answers "
        f"there. Starlink goes only into port 1. With it in ports 2-5 (cameras, FC, laptop) the UGV has no internet and "
        f"no tunnel, and the device search takes phones on the Starlink Wi-Fi for cameras. Move the Starlink cable to "
        f"port 1 and flash again. The router is not flashed"))


def router_addresses(text: str) -> list[tuple[ipaddress.IPv4Interface, str]]:
    """/ip address print terse: адреси роутера з префіксом і інтерфейсом."""
    out = []
    for line in text.splitlines():
        cidr, iface = CIDR_RE.search(line), IFACE_RE.search(line)
        if cidr and iface:
            out.append((ipaddress.ip_interface(cidr[1]), iface[1]))
    return out


def probe_plan(bridge: str, addresses: list[tuple[ipaddress.IPv4Interface, str]], hints: list[str],
               taken: set[str]) -> list[tuple[ipaddress.IPv4Network, str | None]]:
    """Мережі /24, які опитуємо активно, і тимчасова адреса роутера в кожній (None — роутер уже в ній, на мосту).

    Мережі, що вже є на інших інтерфейсах (Starlink на ether1, тунелі), не чіпаємо: тимчасова адреса на мосту
    зламала б маршрут туди — а через тунель ми саме й прошиваємо.
    """
    plan: list[tuple[ipaddress.IPv4Network, str | None]] = []
    for net in dict.fromkeys(ipaddress.ip_network(f"{ip}/24", strict=False) for ip in hints):
        if not net.is_private or any(net.overlaps(ipaddress.ip_network(n)) for n in NO_DEVICE_NETS):
            continue
        if any(a.network.overlaps(net) for a, iface in addresses if iface != bridge):
            continue
        if any(a.network.overlaps(net) for a, iface in addresses if iface == bridge):
            plan.append((net, None))
            continue
        # .1 — зазвичай шлюз старої бортової мережі, якого вже немає; інакше вільна з кінця мережі
        temp = next((str(net.network_address + n) for n in (1, 254, 250, 240) if str(net.network_address + n) not in taken), None)
        if temp:
            plan.append((net, temp))
    return plan


def detect_devices(mt: MikroTik, ports: dict[str, str], log=print, scan_seconds: int = 10,
                   hints: list[str] = (), wait_seconds: float = DEVICE_WAIT) -> dict[str, dict[str, str]]:
    """Хто стоїть у портах камер і ФК і на якій адресі — до прошивки, щоб роутер одразу вів тунель куди треба.

    Спершу слухаємо мережу (/tool ip-scan без адрес): так видно пристрої з будь-якої мережі, якщо вони щось шлють.
    Але камера, яку ніхто не дивиться, може мовчати хвилинами. Тож тих, кого не почули, питаємо самі: роутер
    бере тимчасову адресу в імовірній мережі (де вже знайшлися інші пристрої, адреси з минулої прошивки,
    заводська мережа камер — hints) і опитує всі 254 адреси; на ARP пристрій відповідає, навіть коли мовчить.
    Пристрої з лінком, що ще вантажаться (увімкнулися разом із роутером), чекаємо до wait_seconds.
    """
    bridge = mt.get(f"/interface bridge port get [find interface={ports['cam1']}] bridge")
    if not re.fullmatch(r"[\w.-]+", bridge):
        log(T(f"  порт {ports['cam1']} не в мості — пристрої не шукаю, лишаю адреси, що були",
              f"  port {ports['cam1']} is not in a bridge - not looking for devices, keeping the previous addresses"))
        return {}
    log(T("  шукаю камери й ФК у портах роутера...", "  looking for the cameras and FC on the router ports..."))
    mt.run(f"/ip address remove [find comment={PROBE_COMMENT}]", check=False)  # лишки обірваного минулого пошуку
    deadline = time.monotonic() + wait_seconds
    heard = hosts_text = ""
    links: dict[str, bool] = {}
    quiet = lambda *_: None  # noqa: E731 — проміжні результати не друкуємо

    def look() -> tuple[dict, dict, set[str]]:
        nonlocal hosts_text
        hosts_text += mt.run("/interface bridge host print terse where !local", check=False)  # після опиту: хто озвався
        ip_macs = parse_ip_macs(heard + "\n" + mt.run("/ip arp print terse", check=False))
        own = set(ADDRESS_RE.findall(mt.run("/ip address print terse", check=False)))
        hosts = parse_bridge_hosts(hosts_text)
        wrong = starlink_on_lan(hosts, ip_macs, own)
        if wrong:  # далі шукати марно: що б не знайшлося, НРК з таким підключенням без зв'язку
            raise starlink_on_lan_error(*wrong)
        return hosts, ip_macs, own

    while True:
        heard += mt.run(f"/tool ip-scan interface={bridge} duration={scan_seconds}s", check=False, timeout=scan_seconds + 30)
        hosts, ip_macs, own = look()
        found = pick_devices(ports, hosts, ip_macs, own, log=quiet)
        if len(found) == len(A.DEVICES):
            break
        # Мережі, де варто шукати решту: де вже стоять пристрої в портах, де вони були минулого разу, заводські
        near = [ip for host in A.DEVICES for mac in hosts.get(ports.get(host), []) for ip in ip_macs.get(mac, [])]
        taken = {ip for ips in ip_macs.values() for ip in ips} | set(hints)
        addresses = router_addresses(mt.run("/ip address print terse", check=False))
        plan = probe_plan(bridge, addresses, [*near, *hints, str(A.lan_ip("router"))], taken)
        if plan:
            log(T(f"  не всі озвалися — опитую мережі {', '.join(str(n) for n, _ in plan)}...",
                  f"  not everyone answered - probing the networks {', '.join(str(n) for n, _ in plan)}..."))
        try:
            for net, temp in plan:
                if temp:
                    mt.run(f"/ip address add address={temp}/24 interface={bridge} comment={PROBE_COMMENT}", check=False)
                heard += mt.run(f"/tool ip-scan interface={bridge} address-range={net} duration={PROBE_SECONDS}s",
                                check=False, timeout=PROBE_SECONDS + 30)
            hosts, ip_macs, own = look()  # тимчасові адреси теж у own: це адреси роутера, не пристроїв
        finally:
            if any(temp for _, temp in plan):
                mt.run(f"/ip address remove [find comment={PROBE_COMMENT}]", check=False)
        found = pick_devices(ports, hosts, ip_macs, own, log=quiet)
        missing = [h for h in A.DEVICES if h not in found]
        links = {h: mt.get(f"/interface get [find name={ports.get(h)}] running") == "true" for h in missing}
        if not missing or time.monotonic() >= deadline or not any(links.values()):
            break
        names = ", ".join(T(*DEVICE_LABELS[h]) for h in missing if links[h])
        log(T(f"  {names}: лінк є, а відповіді немає — мабуть, ще вантажиться, чекаю...",
              f"  {names}: the link is up but no answer - probably still booting, waiting..."))
    return pick_devices(ports, hosts, ip_macs, own, log=log, links=links)


def apply_full_config(mt: MikroTik, rsc: Path) -> None:
    """Скидання роутера з нашим конфігом замість заводського. Роутер іде в перезавантаження."""
    remote = ("flash/" if mt.has_flash() else "") + "fleet-robot.rsc"
    mt.upload(rsc, remote)
    wait_for_file(mt, remote, size=rsc.stat().st_size)
    # RouterOS 7.20: файл уже є в /file, а run-after-reset ще кілька секунд його не приймає
    # («input does not match any value of run-after-reset») — чекаємо й пробуємо знову
    for attempt in range(RESET_ATTEMPTS):
        try:
            mt.run(
                f"/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset={remote}",
                timeout=15, stdin_text="y\n",
            )
            return
        except (socket.timeout, paramiko.SSHException, EOFError, OSError):
            return  # з'єднання рветься через перезавантаження — це нормально
        except MikroTikError as exc:
            if "run-after-reset" in str(exc) and attempt < RESET_ATTEMPTS - 1:
                time.sleep(3)
                continue
            files = mt.run("/file print terse", check=False).strip()
            raise MikroTikError(T(f"{exc}\nФайли на роутері:\n{files}", f"{exc}\nFiles on the router:\n{files}")) from None


def wait_for(hosts: str | list[str], password: str, timeout: float = 300) -> MikroTik:
    """Перша адреса, на якій роутер піднявся з нашим паролем (пробуємо всі по колу)."""
    hosts = [hosts] if isinstance(hosts, str) else hosts
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        for host in hosts:
            try:
                return MikroTik(host, password, timeout=5)
            except MikroTikError as exc:
                last = exc
        time.sleep(5)
    listed = ", ".join(hosts)
    raise MikroTikError(T(f"MikroTik {listed} не повернувся за {int(timeout)} с: {last}",
                          f"MikroTik {listed} did not come back within {int(timeout)} s: {last}"))


UPLINK_WAIT = 90  # скільки після прошивки чекати рукопожаття з сервером, коли Starlink у порту 1
INTERNET_PROBE = "1.1.1.1"  # на ping відповідає завжди; сервер майданчика ICMP може й не пропускати


def check_uplink(mt: MikroTik, wan: str, tag: str, server: str, wait: float = UPLINK_WAIT,
                 log=print) -> tuple[str, str]:
    """Чи вийшов щойно прошитий НРК на сервер — щоб «прошито ✓» не означало «на зв'язку» наосліп (поле 29.10).

    (стан, повідомлення). online — рукопожаття є; no_wan — від Starlink у порту 1 адреси немає (на стенді звична
    справа: зв'язок перевіряють у кроці 6); no_internet — адреса є, а інтернету немає; no_handshake — інтернет є,
    а сервер тунель не приймає (НРК на ньому немає чи ключ інший).
    """
    if mt.get(f"/ip dhcp-client get [find interface={wan}] status") != "bound":
        return "no_wan", T(
            f"зв'язок із сервером не перевірено: від Starlink у порту 1 адреси немає (не під'єднано?). Під'єднайте "
            f"Starlink у порт 1 і перевірте зв'язок у кроці 6",
            f"the link to the server is not checked: no address from Starlink in port 1 (not connected?). Connect "
            f"Starlink to port 1 and check the link in step 6")
    address = mt.get(f"/ip dhcp-client get [find interface={wan}] address")
    log(T(f"  Starlink у порту 1 дав адресу {address} — чекаю рукопожаття з сервером {server} (до {int(wait)} с)...",
            f"  Starlink in port 1 gave address {address} - waiting for a handshake with the server {server} "
            f"(up to {int(wait)} s)..."))
    deadline = time.monotonic() + wait
    while True:
        age = mt.last_handshake(tag)
        if age is not None:
            return "online", T(f"зв'язок із сервером є: рукопожаття {age} с тому ✓",
                               f"the server link is up: handshake {age} s ago ✓")
        if time.monotonic() >= deadline:
            break
        time.sleep(5)
    received = re.findall(r"\breceived=(\d+)", mt.run(f"/ping {INTERNET_PROBE} count=3", check=False, timeout=30))
    if not (received and int(received[-1]) > 0):
        return "no_internet", T(
            f"УВАГА: НРК НЕ НА ЗВ'ЯЗКУ. Starlink у порту 1 дав адресу {address}, але інтернету немає ({INTERNET_PROBE} "
            f"не відповідає). Перевірте Starlink (у застосунку Starlink — «Підключено»), тоді крок 6",
            f"WARNING: THE UGV IS OFFLINE. Starlink in port 1 gave address {address}, but there is no internet "
            f"({INTERNET_PROBE} does not answer). Check Starlink (\"Connected\" in the Starlink app), then step 6")
    return "no_handshake", T(
        f"УВАГА: НРК НЕ НА ЗВ'ЯЗКУ. Інтернет є, а сервер {server} за {int(wait)} с рукопожаття не дав: на сервері немає "
        f"цього НРК або в нього інший ключ. Виконайте на сервері команду з кроку 2 ще раз; на сервері sudo wg show — "
        f"у піра цього НРК має з'явитися latest handshake",
        f"WARNING: THE UGV IS OFFLINE. There is internet, but the server {server} gave no handshake in {int(wait)} s: "
        f"the server does not have this UGV or has another key. Run the step 2 command on the server again; on the "
        f"server sudo wg show - the peer of this UGV must get a latest handshake")


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
