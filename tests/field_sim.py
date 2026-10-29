"""Модель стенда для прогону справжньої прошивки (fleet bench mikrotik) через стани, у яких бував НРК у полі.

Роутер НРК: ether1 — WAN, ether2–5 — міст. У порту може стояти що завгодно: камери, адаптер ФК, ноутбук, а може й
Starlink — тоді за цим портом і його роутер 192.168.1.1, і всі клієнти його Wi-Fi (телефони з випадковими MAC).
Модель відповідає на ті самі команди RouterOS, що й справжній роутер (ip-scan, bridge host, arp, ip address), тож
пошук пристроїв іде справжнім кодом. Заливання конфігу й повернення роутера після скидання підмінено: конфіг
запам'ятовуємо, а «чи вийшов би НРК на зв'язок» вирішує outcome() — за конфігом і тим, що стоїть у порту 1.

Модуль не залежить від версії програми: ним же порівнюємо 0.3.3 і новішу (див. CHANGELOG 0.3.4).
"""
from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

STARLINK_NET = ipaddress.ip_network("192.168.1.0/24")
SERVER_KEY = "R7siiKlSPShq3oy7Rqn7QUxuRBtbDbl3dsFEEMxzpGE="  # відкритий ключ сервера hub із поля

# MAC із поля 29.10 (НРК pg0505): камери Dahua й адаптер ФК — заводські; телефон і «пристрій» у порту 3 — випадкові
FRONT, REAR, FC = "8C:E9:B4:6B:D9:A9", "8C:E9:B4:6B:D9:B0", "3C:AB:72:52:62:BA"
PHONE, GADGET, LAPTOP, STARLINK = "72:21:4F:53:CC:C1", "26:12:AC:1A:80:01", "3C:7C:3F:00:11:22", "74:24:9F:91:DA:C3"


@dataclass
class Host:
    mac: str
    ip: str | None
    chatty: bool = True  # шле сам — чути, просто слухаючи (ip-scan без адрес)
    arp: bool = True     # відповідає на ARP


def front() -> Host:
    return Host(FRONT, "192.168.15.108")


def rear(ip: str = "192.168.15.109", chatty: bool = False) -> Host:
    return Host(REAR, ip, chatty=chatty)  # задня камера, яку ніхто не дивиться, мовчить


def fc() -> Host:
    return Host(FC, "192.168.15.253")


def laptop() -> Host:
    return Host(LAPTOP, "192.168.88.254")


def starlink(chatty: bool = True) -> list[Host]:
    """Роутер Starlink і телефони в його Wi-Fi: за портом, у який воткнули Starlink, видно всіх."""
    return [Host(PHONE, "192.168.1.68"), Host(STARLINK, "192.168.1.1", chatty=chatty), Host("2A:11:22:33:44:55", "192.168.1.82")]


class Bench:
    BRIDGE_PORTS = ("ether2", "ether3", "ether4", "ether5")

    def __init__(self, ports: dict[str, list[Host]], laptop_lost: bool = False, internet: bool = True,
                 server_knows: bool = True):
        self.ports = ports
        self.internet = internet          # у Starlink є інтернет
        self.server_knows = server_knows  # на сервері є пір із ключем цього НРК (команду кроку 2 виконано)
        # Після скидання ноутбук у порту 5 узяв адресу від Starlink за мостом і до 192.168.88.1 не дістає. Це гонка
        # двох DHCP: 27.10 ноутбук лишився з роутером («Роутер прошито ✓»), 29.10 о 14:36 — ні
        self.laptop_lost = laptop_lost
        self.bridge = "bridge"  # заводський конфіг; після нашого — br-lan
        self.temp: list[str] = []
        self.heard: set[str] = set()
        self.arp: dict[str, str] = {}
        self.rsc: str | None = None

    # --- що стоїть де
    def on_bridge(self) -> list[tuple[str, Host]]:
        return [(p, h) for p in self.BRIDGE_PORTS for h in self.ports.get(p, [])]

    @staticmethod
    def has_starlink(hosts: list[Host]) -> bool:
        return any(h.mac == STARLINK for h in hosts)

    def starlink_on_wan(self) -> bool:
        return self.has_starlink(self.ports.get("ether1", []))

    def starlink_on_bridge(self) -> bool:
        return self.has_starlink([h for _, h in self.on_bridge()])

    def config_breaks_wan(self) -> bool:
        """Наш конфіг дав мосту адресу в мережі Starlink — шлюз Starlink роутер шукає в себе за мостом."""
        return any(a.network.overlaps(STARLINK_NET) for a in lan_addresses(self.rsc or ""))

    def script_aborts(self) -> bool:
        """Друга оренда DHCP з тією ж MAC чи адресою — RouterOS зупиняє скрипт run-after-reset на ній: далі ні WAN,
        ні тунелю, ні відбитка (поле 26.10: адаптер ФК переїхав з порту камери)."""
        return dup_lease(self.rsc or "") is not None

    def has_internet(self) -> bool:
        return (self.rsc is not None and self.starlink_on_wan() and self.internet and not self.config_breaks_wan()
                and not self.script_aborts())

    def last_handshake(self, tag: str) -> int | None:
        return 12 if self.has_internet() and self.server_knows and tag == "fleet-site:main" else None

    def addresses(self) -> list[tuple[str, str]]:
        own = [("192.168.88.1/24", self.bridge)]
        if self.starlink_on_wan():
            own.append(("192.168.1.37/24", "ether1"))  # DHCP від Starlink
        return own + [(a, self.bridge) for a in self.temp]

    # --- RouterOS
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def get(self, expr: str) -> str:
        if "bridge port get" in expr:
            return self.bridge
        if "running" in expr:
            return "true" if self.ports.get(re.search(r"name=(\w+)", expr)[1]) else "false"
        if "fleet-version" in expr:
            m = re.search(r'comment="(fleet [0-9a-f]+)"', self.rsc or "")
            return m[1] if m and not self.script_aborts() else ""
        if "dhcp-client get" in expr:
            if not self.starlink_on_wan():
                return "searching..."
            return "bound" if expr.endswith("status") else "192.168.1.37/24"
        return ""

    def run(self, command: str, check: bool = True, timeout: float = 60, stdin_text: str | None = None) -> str:
        if command.startswith("/tool ip-scan"):
            rng = re.search(r"address-range=(\S+)", command)
            if not rng:
                hits = [h for _, h in self.on_bridge() if h.chatty]
            else:
                net = ipaddress.ip_network(rng[1])
                if not any(ipaddress.ip_interface(a).network == net for a, i in self.addresses() if i == self.bridge):
                    return ""  # без своєї адреси в мережі роутер її не опитає
                hits = [h for _, h in self.on_bridge() if h.arp and h.ip and ipaddress.ip_address(h.ip) in net]
                self.arp.update({h.ip: h.mac for h in hits})
            self.heard |= {h.mac for h in hits}
            return "".join(f"{h.ip}  {h.mac}\n" for h in hits if h.ip)
        if command.startswith("/interface bridge host print"):
            return "".join(f" 0 D mac-address={h.mac} on-interface={p} bridge={self.bridge}\n"
                           for p, h in self.on_bridge() if h.mac in self.heard)
        if command.startswith("/ip arp print"):
            wan = f" 0 DC address=192.168.1.1 mac-address={STARLINK} interface=ether1\n" if self.starlink_on_wan() else ""
            return wan + "".join(f" 0 DC address={ip} mac-address={mac} interface={self.bridge}\n" for ip, mac in self.arp.items())
        if command.startswith("/ip address print"):
            return "".join(f"  0   address={a} interface={i}\n" for a, i in self.addresses())
        if command.startswith("/ping"):
            got = 3 if self.has_internet() else 0
            return f"    sent=3 received={got} packet-loss={100 - got * 100 // 3}%\n"
        if command.startswith("/ip address add"):
            self.temp.append(re.search(r"address=(\S+)", command)[1])
        elif command.startswith("/ip address remove"):
            self.temp = []
        return ""

    def fingerprint(self) -> str | None:
        comment = self.get("fleet-version")
        return comment.removeprefix("fleet ") if comment else None

    def export(self) -> str:
        return self.rsc or ""


def field_root(tmp: Path, repo: Path, devices: dict[int, dict] | None = None) -> Path:
    """Інвентар ноутбука з поля: hub main (сервер 13.63.179.54), пілот pilot005, НРК 1 (слот 1) і 3 (pg0505, слот 3)."""
    root = tmp / "field"
    for name in ("base", "templates", "server"):
        shutil.copytree(repo / name, root / name)
    inv = root / "inventory"
    inv.mkdir()
    (inv / "sites.yaml").write_text(
        "sites:\n  main:\n    kind: hub\n    customer: FLU\n    host: 13.63.179.54\n    endpoint: 13.63.179.54:51820\n"
        f"    server_pubkey: \"{SERVER_KEY}\"\n    network:\n      tunnel: 10.150.0.0/24\n"
        "      operators: 10.150.1.0/24\n      robot_lans: 10.150.16.0/20\n", "utf-8")
    (inv / "operators.yaml").write_text(
        "operators:\n  - name: pilot005\n    id: 5\n    role: operator\n    sites: [main]\n", "utf-8")
    robots = {"robots": [
        {"id": 1, "serial": "pg0101", "platform": "flu-v1", "site": "main", "slot": 1, "service_tunnel": False},
        {"id": 2, "serial": "pg5", "platform": "flu-v1", "site": "main", "service_tunnel": False},
        {"id": 3, "serial": "pg0505", "platform": "flu-v1", "site": "main", "slot": 3, "service_tunnel": False}]}
    for item in robots["robots"]:
        if (devices or {}).get(item["id"]):
            item["devices"] = devices[item["id"]]
    (inv / "robots.yaml").write_text(json.dumps(robots, ensure_ascii=False, indent=2), "utf-8")  # JSON — теж YAML
    return root


def flash(root: Path, bench: Bench, robot_id: int = 3, *extra: str) -> dict:
    """fleet bench mikrotik --id N справжнім кодом проти моделі стенда. Що сказала програма і з яким конфігом лишився роутер."""
    from fleet import cli, mikrotik, netcheck
    from fleet.keystore import SecretStore

    store = SecretStore(root)
    for rid in (1, 3):
        store.robot(rid, create=True)

    def apply(mt, rsc):
        bench.rsc, bench.bridge = Path(rsc).read_text("utf-8"), "br-lan"

    def back(hosts, password, timeout=300):
        if bench.laptop_lost:
            raise mikrotik.MikroTikError(f"MikroTik {', '.join(hosts)} не повернувся за {int(timeout)} с: "
                                         f"немає SSH до MikroTik 192.168.88.1: timed out")
        return bench

    ip = "192.168.1.223" if bench.laptop_lost else "192.168.88.201"
    clock = iter(range(0, 10**6, 20))  # кожен погляд на годинник — +20 с: пошук не чекає справжні 90 с
    saved = (cli.ROOT, mikrotik.open_router, mikrotik.apply_full_config, mikrotik.wait_for, netcheck.wired_adapters,
             time.monotonic, time.sleep)
    cli.ROOT = root
    mikrotik.open_router = lambda host, passwords: bench
    mikrotik.apply_full_config, mikrotik.wait_for = apply, back
    netcheck.wired_adapters = lambda: [{"name": "Ethernet", "up": True, "dhcp": True, "ips": [ip]}]
    time.monotonic, time.sleep = (lambda: next(clock)), (lambda s: None)
    out, result = io.StringIO(), {}
    try:
        with contextlib.redirect_stdout(out):
            args = cli.build_parser().parse_args(["bench", "mikrotik", "--id", str(robot_id), "--host", "192.168.88.1",
                                                  "--password", "", *extra])
            result["exit"] = args.fn(args)
    except Exception as exc:  # noqa: BLE001 — так програма закінчується «помилка: ...»
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (cli.ROOT, mikrotik.open_router, mikrotik.apply_full_config, mikrotik.wait_for, netcheck.wired_adapters,
         time.monotonic, time.sleep) = saved
    result["log"], result["rsc"] = out.getvalue(), bench.rsc
    return result


def lan_addresses(rsc: str) -> list[ipaddress.IPv4Interface]:
    return [ipaddress.ip_interface(a) for a in re.findall(r"/ip address add address=(\S+) interface=br-lan", rsc)]


def dup_lease(rsc: str) -> str | None:
    """MAC чи адреса, що двічі трапляється в орендах DHCP конфігу (None — дублів немає)."""
    seen: set[str] = set()
    for mac, ip in re.findall(r"/ip dhcp-server lease add \S+ mac-address=(\S+) address=(\S+)", rsc):
        for value in (mac, ip):
            if value in seen:
                return value
            seen.add(value)
    return None


def outcome(result: dict, bench: Bench) -> str:
    """Чи вийшов би НРК на зв'язок, коли Starlink нарешті в порту 1 (як і має бути в полі)."""
    if result.get("rsc") is None:
        return "не прошито"
    bad = [a for a in lan_addresses(result["rsc"]) if a.network.overlaps(STARLINK_NET)]
    if bad:
        return f"без зв'язку: роутер узяв {bad[0].ip} у мережі Starlink"
    if dup_lease(result["rsc"]):
        return f"без зв'язку: конфіг обірветься на дублі оренди DHCP ({dup_lease(result['rsc'])})"
    if not bench.internet:
        return "без зв'язку: у Starlink немає інтернету"
    if not bench.server_knows:
        return "без зв'язку: сервер не знає цього НРК"
    return "на зв'язку"


# Стан бортової мережі з поля (robots.yaml НРК 3 на ноутбуці РУЛЕТКА)
FIELD_DEVICES = {"fc": {"ip": "192.168.15.253", "mac": FC}, "cam1": {"ip": "192.168.15.108", "mac": FRONT},
                 "cam2": {"ip": "192.168.1.68", "mac": PHONE}}
WITHOUT_PHONE = {h: d for h, d in FIELD_DEVICES.items() if h != "cam2"}
# Поле 26.10: минула прошивка знайшла адаптер ФК у порту 3 — і записала його задньою камерою
FC_WAS_REAR = {"cam1": {"ip": "192.168.15.108", "mac": FRONT}, "cam2": {"ip": "192.168.15.253", "mac": FC}}


def silent_front() -> Host:
    return Host(FRONT, "192.168.15.108", chatty=False, arp=False)  # лінк є, а ще не відповідає (вантажиться)


def silent_rear() -> Host:
    return Host(REAR, "192.168.15.109", chatty=False, arp=False)


@dataclass
class Scenario:
    key: str
    title: str
    bench: callable
    devices: dict | None = None  # {id НРК: devices у robots.yaml до прошивки}
    extra: tuple = ()
    robot: int = 3


SCENARIOS = [
    Scenario("A", "27.10, перша прошивка: Starlink у порту 3 замість задньої камери (камера — у порту 1)",
             lambda: Bench({"ether1": [rear()], "ether2": [front()], "ether3": starlink(), "ether4": [fc()],
                            "ether5": [laptop()]})),
    Scenario("B", "29.10 13:58: роутер скинуто, Starlink від'єднано, задньої камери немає; у robots.yaml «камера» "
                  "192.168.1.68 з минулого разу",
             lambda: Bench({"ether2": [front()], "ether4": [fc()], "ether5": [laptop()]}), {3: FIELD_DEVICES}),
    Scenario("B2", "те саме, «Прошити без них»",
             lambda: Bench({"ether2": [front()], "ether4": [fc()], "ether5": [laptop()]}), {3: FIELD_DEVICES},
             ("--allow-missing",)),
    Scenario("C", "29.10 14:36: у порту 3 пристрій 26:12:… без адреси, Starlink за мостом (свитч у порту 5 із "
                  "ноутбуком), «Прошити без них»",
             lambda: Bench({"ether2": [front()], "ether3": [Host(GADGET, None)], "ether4": [fc()],
                            "ether5": [laptop(), *starlink()]}, laptop_lost=True), {3: WITHOUT_PHONE},
             ("--allow-missing",)),
    Scenario("D", "Starlink і в порту 1, і другим кабелем у свитч на порту 5",
             lambda: Bench({"ether1": [Host(STARLINK, "192.168.1.1")], "ether2": [front()], "ether3": [rear()],
                            "ether4": [fc()], "ether5": [laptop(), *starlink()]}, laptop_lost=True), {3: WITHOUT_PHONE}),
    Scenario("E", "новий борт: передня камера на заводській 192.168.1.108, Starlink від'єднано",
             lambda: Bench({"ether2": [Host(FRONT, "192.168.1.108")], "ether3": [rear()], "ether4": [fc()],
                            "ether5": [laptop()]})),
    Scenario("E2", "те саме, «Прошити без них»",
             lambda: Bench({"ether2": [Host(FRONT, "192.168.1.108")], "ether3": [rear()], "ether4": [fc()],
                            "ether5": [laptop()]}), None, ("--allow-missing",)),
    Scenario("F", "борт 1: у robots.yaml та сама «камера» 192.168.1.68, а справжня задня камера на місці",
             lambda: Bench({"ether2": [front()], "ether3": [rear()], "ether4": [fc()], "ether5": [laptop()]}),
             {1: FIELD_DEVICES}, (), 1),
    Scenario("G", "усе як треба: Starlink у порту 1, камери й ФК у своїх портах",
             lambda: Bench({"ether1": [Host(STARLINK, "192.168.1.1")], "ether2": [front()], "ether3": [rear()],
                            "ether4": [fc()], "ether5": [laptop()]}), {3: FIELD_DEVICES}),
    Scenario("J", "26.10: адаптер ФК минулого разу стояв у порту 3 (записався задньою камерою .253), тепер у порту 4; "
                  "камери ще вантажаться й мовчать",
             lambda: Bench({"ether2": [silent_front()], "ether3": [silent_rear()], "ether4": [fc()],
                            "ether5": [laptop()]}), {3: FC_WAS_REAR}),
    Scenario("J2", "те саме, «Прошити без них»",
             lambda: Bench({"ether2": [silent_front()], "ether3": [silent_rear()], "ether4": [fc()],
                            "ether5": [laptop()]}), {3: FC_WAS_REAR}, ("--allow-missing",)),
    Scenario("H", "Starlink у порту 1, але без інтернету",
             lambda: Bench({"ether1": [Host(STARLINK, "192.168.1.1")], "ether2": [front()], "ether3": [rear()],
                            "ether4": [fc()], "ether5": [laptop()]}, internet=False), {3: WITHOUT_PHONE}),
    Scenario("I", "Starlink у порту 1 з інтернетом, а на сервері цього НРК немає (команду кроку 2 не виконано)",
             lambda: Bench({"ether1": [Host(STARLINK, "192.168.1.1")], "ether2": [front()], "ether3": [rear()],
                            "ether4": [fc()], "ether5": [laptop()]}, server_knows=False), {3: WITHOUT_PHONE}),
]


def said(result: dict) -> str:
    """Що людина побачила наприкінці: помилку, «пристроїв бракує» чи «готово»."""
    if result.get("error"):
        return result["error"].split(": ", 1)[1].splitlines()[0][:150] + (" …" if "\n" in result["error"] else "")
    done = {0: "готово", 3: "не прошито: бракує пристроїв", 4: "прошито, але НРК не на зв'язку"}
    said = done.get(result.get("exit"), f"код {result.get('exit')}")
    if result.get("exit") in (0, 4):  # що програма сказала про зв'язок із сервером (0.3.4+)
        link = [line for line in result["log"].splitlines() if "зв'язок із сервером" in line or "НЕ НА ЗВ'ЯЗКУ" in line]
        said += f" — {link[-1].strip()[:140]}" if link else ""
    return said


def run_all(tmp: Path, repo: Path) -> list[dict]:
    rows = []
    for s in SCENARIOS:
        root = field_root(tmp / s.key, repo, s.devices)
        bench = s.bench()
        result = flash(root, bench, s.robot, *s.extra)
        rows.append({"key": s.key, "title": s.title, "said": said(result), "outcome": outcome(result, bench),
                     "result": result, "root": root})
    return rows
