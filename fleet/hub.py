"""Сервер «hub» з однієї команди: пілот вставляє її на сервері й копіює відповідь назад.

1. prepare()  — ключі пілота й НРК створюються на ноутбуці; в команду йдуть лише відкриті ключі.
2. command()  — одна команда для сервера: скрипт server/hub/flu-hub.sh, стиснутий у base64.
               Скрипт сам ставить WireGuard, якщо його немає, і додає пілота й НРК.
3. apply_response() — рядок FLU1:...:END із сервера -> інвентар (адреса й ключ сервера,
               номер пілота, слот НРК) і конфіги: тунель пілота й конфіг роутера.
               Правий сервер: запис цього ноутбука з тим самим номером чи слотом застарів і звільняється.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import ipaddress
import json
import re
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import addressing as A
from . import feature
from .i18n import T
from .inventory import NAME_RE, Inventory, InventoryError, _yaml, is_wg_key
from .keystore import SecretStore, wg_pubkey
from .render import Renderer

SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
RESPONSE_RE = re.compile(r"FLU1:([A-Za-z0-9+/=\s]+?):END")
CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
SCRIPT = Path("server") / "hub" / "flu-hub.sh"


class HubError(Exception):
    pass


# --- інвентар ------------------------------------------------------------------

def _edit(path: Path, change) -> None:
    y = _yaml()
    doc = y.load(path.read_text("utf-8")) or CommentedMap()
    change(doc)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        y.dump(doc, f)


def hub_site(inv: Inventory, name: str | None = None):
    site = inv.site(name) if name else next((s for s in inv.sites.values() if s.hub), None)
    if site is None or not site.hub:
        raise HubError(T("в inventory/sites.yaml немає hub-майданчика", "there is no hub site in inventory/sites.yaml"))
    return site


def ensure_robot(root: Path, inv: Inventory, site: str, serial: str) -> int:
    """id НРК із таким заводським номером; якщо такого ще немає — додати в robots.yaml."""
    for r in inv.robots.values():
        if r.serial == serial:
            return r.id
    robot_id = max(inv.robots, default=0) + 1
    platform = next(iter(inv.platforms))

    def change(doc):
        if doc.get("robots") is None:
            doc["robots"] = CommentedSeq()
        doc["robots"].append(CommentedMap([
            ("id", robot_id), ("serial", serial), ("platform", platform), ("site", site), ("service_tunnel", False),
        ]))
    _edit(root / "inventory" / "robots.yaml", change)
    return robot_id


def _set_site(root: Path, site: str, data: dict) -> None:
    host, _, port = data["endpoint"].rpartition(":")

    def change(doc):
        entry = doc["sites"][site]
        entry["kind"] = "hub"
        entry["host"] = host
        entry["endpoint"] = f"{host}:{port}"
        entry["server_pubkey"] = data["server_pubkey"]
        net = entry.get("network")
        if net is None:
            net = entry["network"] = CommentedMap()
        net["tunnel"], net["operators"], net["robot_lans"] = data["tunnel"], data["operators"], data["robot_lans"]
    _edit(root / "inventory" / "sites.yaml", change)


def _set_pilot(root: Path, site: str, name: str, pilot_id: int) -> None:
    def change(doc):
        if doc.get("operators") is None:
            doc["operators"] = CommentedSeq()
        for item in doc["operators"]:
            if item["name"] == name:
                item["id"] = pilot_id
                if site not in item["sites"]:
                    item["sites"].append(site)
                return
        sites = CommentedSeq([site])
        sites.fa.set_flow_style()
        doc["operators"].append(CommentedMap([("name", name), ("id", pilot_id), ("role", "dispatcher"), ("sites", sites)]))
    _edit(root / "inventory" / "operators.yaml", change)


def _release(root: Path, site: str, pilot: str, pilot_id: int, robot_id: int | None, slot: int | None) -> list[str]:
    """Сервер видає лише номер пілота й слот НРК, яких у його базі ні в кого немає. Якщо в інвентарі цього
    ноутбука вони записані за іншим пілотом чи НРК, той запис застарів: на сервері їх уже немає (сервер
    новий чи перевстановлений або їх із нього видалили). Звільняємо: пілот лишається без майданчика,
    НРК — без слота. Повертає, що звільнено, — людині."""
    notes: list[str] = []

    def free_pilot(doc):
        for item in doc.get("operators") or []:
            if item["name"] != pilot and int(item["id"]) == pilot_id and site in (item.get("sites") or []):
                item["sites"].remove(site)
                notes.append(T(f"пілот {item['name']}: його номер {pilot_id} сервер видав пілоту {pilot} — на цьому сервері "
                               f"{item['name']} немає. Повернути: виберіть його в кроці 1 і виконайте команду на сервері ще раз",
                               f"pilot {item['name']}: the server gave its number {pilot_id} to pilot {pilot} - {item['name']} "
                               f"is not on this server. To bring it back: choose it in step 1 and run the command on the server again"))

    def free_robot(doc):
        for item in doc.get("robots") or []:
            if int(item["id"]) != robot_id and item.get("site") == site and item.get("slot") is not None and int(item["slot"]) == slot:
                del item["slot"]
                name = f"nrk-{int(item['id']):03d}"
                notes.append(T(f"НРК {name}: його слот {slot} сервер видав іншому НРК — на цьому сервері {name} немає. "
                               f"Повернути: додайте його знову (крок 1) і перепрошийте роутер",
                               f"UGV {name}: the server gave its slot {slot} to another UGV - {name} is not on this server. "
                               f"To bring it back: add it again (step 1) and reflash the router"))

    _edit(root / "inventory" / "operators.yaml", free_pilot)
    if robot_id is not None:
        _edit(root / "inventory" / "robots.yaml", free_robot)
    return notes


# --- команда для сервера ---------------------------------------------------------

def latin_hint(value: str | None) -> str:
    """Підказка, якщо в значенні кирилиця: «П» і «P» схожі, а «ПФ-01» і «PF-01» — різні номери."""
    letters = "".join(dict.fromkeys(CYRILLIC_RE.findall(value or "")))
    return T(f" Тут є кирилиця ({letters}) — наберіть латиницею.", f" It contains Cyrillic ({letters}) - type it in Latin.") if letters else ""


def check_pilot(pilot: str | None) -> None:
    if not NAME_RE.match(pilot or ""):
        raise HubError(T("ім'я пілота: латиниця a-z, цифри й '-', до 20 символів (наприклад, ivan).",
                         "pilot name: latin a-z, digits and '-', up to 20 characters (e.g. ivan).") + latin_hint(pilot))


def check_serial(serial: str | None) -> None:
    if not SERIAL_RE.match(serial or ""):
        raise HubError(T("номер НРК: латиниця, цифри, '.', '_' і '-', до 40 символів (наприклад, PF-01).",
                         "UGV number: latin letters, digits, '.', '_' and '-', up to 40 characters (e.g. PF-01).") + latin_hint(serial))


def validate(pilot: str, serial: str | None) -> None:
    check_pilot(pilot)
    if serial is not None:
        check_serial(serial)


def prepare(root: Path, pilot: str, serial: str | None) -> dict:
    """Ключі пілота й НРК на ноутбуці + НРК в robots.yaml. Повторний виклик нічого не ламає."""
    validate(pilot, serial)
    inv = Inventory.load(root)
    site = hub_site(inv)
    store = SecretStore(root)
    # Чужі імена й номери НРК перевіряє сервер (flu-hub.sh, check_owner): він бачить усі ноутбуки
    store.operator(pilot)
    robot_id = None
    if serial:
        robot_id = ensure_robot(root, inv, site.name, serial)
        store.robot(robot_id, create=True)
    return {"pilot": pilot, "robot_id": robot_id, "command": command(root, pilot, robot_id)}


def command(root: Path, pilot: str, robot_id: int | None = None) -> str:
    store = SecretStore(root)
    args = [pilot, wg_pubkey(store.operator(pilot)["wg"])]
    if robot_id is not None:
        robot = Inventory.load(root).robot(robot_id)
        args += [robot.serial, wg_pubkey(store.robot(robot_id)["wg_site"])]
    if not feature("give"):  # передачу ще не перевірено: НРК іншого пілота сервер не віддасть
        args.insert(0, "--no-give")
    return server_command(root, args)


def admin_command(root: Path, action: str, serial: str | None = None, pilot: str | None = None) -> str:
    """Службова команда для сервера. Ключі не потрібні: НРК і пілотів сервер знаходить за іменами.

    give — передати НРК пілоту; remove-pilot — видалити пілота; remove-robot — видалити НРК і звільнити слот.
    """
    if action == "give":
        if not feature("give"):
            raise HubError(T("передача НРК у цій версії програми вимкнена", "giving a UGV is turned off in this version"))
        check_serial(serial)
        check_pilot(pilot)
        return server_command(root, ["--give", serial, pilot])
    if action == "remove-pilot":
        check_pilot(pilot)
        return server_command(root, ["--remove-pilot", pilot])
    if action == "remove-robot":
        check_serial(serial)
        return server_command(root, ["--remove-robot", serial])
    raise HubError(T(f"невідома дія: {action}", f"unknown action: {action}"))


def server_command(root: Path, args: list[str]) -> str:
    """Одна команда для вставки на сервері: скрипт flu-hub.sh, стиснутий у base64, і його аргументи."""
    return packed_command(root / SCRIPT, args)


def packed_command(path: Path, args: list[str]) -> str:
    """Скрипт у один рядок для вставки в консоль: стиснутий у base64, з аргументами.

    Рядки-коментарі потрібні людям у репозиторії, а не серверу: без них команда коротша.
    Рядок довший за ~8 тис. символів обрізають деякі консолі (і Git Bash, коли його запускає Windows).
    """
    lines = path.read_bytes().replace(b"\r\n", b"\n").decode().split("\n")
    script = "\n".join(line for i, line in enumerate(lines) if i == 0 or not line.lstrip().startswith("#")).encode()
    payload = base64.b64encode(gzip.compress(script, mtime=0)).decode()
    quoted = " ".join(f"'{a}'" for a in args)  # у значеннях немає апострофів: їх перевіряє той, хто викликає
    return f"echo {payload} | base64 -d | gunzip > /tmp/{path.name} && bash /tmp/{path.name} {quoted}"


# --- відповідь сервера -----------------------------------------------------------

def parse_response(text: str) -> dict:
    m = RESPONSE_RE.search(text or "")
    if not m:
        raise HubError(T("не знайшов рядок FLU1:...:END — скопіюйте його з сервера повністю",
                         "the FLU1:...:END line was not found - copy it from the server completely"))
    try:
        data = json.loads(base64.b64decode(re.sub(r"\s+", "", m.group(1)), validate=True))
        host, _, port = data["endpoint"].rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("endpoint")
        for key in ("tunnel", "operators", "robot_lans"):
            ipaddress.ip_network(data[key])
        if data.get("v") != 1 or not is_wg_key(data["server_pubkey"]):
            raise ValueError("version")
        int(data["pilot"]["id"])
        if len(data["pilot"]["key"]) != 8 or (data.get("robot") and len(data["robot"]["key"]) != 8):
            raise ValueError("key")
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise HubError(T("рядок скопійовано не повністю або з помилкою — скопіюйте його ще раз",
                         "the line is incomplete or damaged - copy it again")) from None
    return data


def apply_response(root: Path, text: str) -> dict:
    """Записати відповідь сервера в інвентар і зібрати конфіги пілота та НРК."""
    data = parse_response(text)
    inv = Inventory.load(root)
    site = hub_site(inv)
    store = SecretStore(root)

    pilot = data["pilot"]["name"]
    pilot_file = store.dir / "operators" / f"{pilot}.json"
    if not pilot_file.exists() or not wg_pubkey(store.operator(pilot)["wg"]).startswith(data["pilot"]["key"]):
        raise HubError(T(f"ця відповідь для іншого пілота або ноутбука ({pilot}): підготуйте команду тут і запустіть її ще раз",
                         f"this response is for another pilot or laptop ({pilot}): prepare the command here and run it again"))

    robot = None
    if data.get("robot"):
        robot = next((r for r in inv.robots.values() if r.serial == data["robot"]["serial"]), None)
        if robot is None or not wg_pubkey(store.robot(robot.id)["wg_site"]).startswith(data["robot"]["key"]):
            raise HubError(T(f"НРК {data['robot']['serial']} у відповіді не збігається з підготовленим на цьому ноутбуці",
                             f"UGV {data['robot']['serial']} in the response does not match the one prepared on this laptop"))

    # Не пройшла перевірка — інвентар лишається, яким був: інакше в ньому застрягла б половина відповіді
    files = [root / "inventory" / f"{name}.yaml" for name in ("sites", "operators", "robots")]
    before = {path: path.read_bytes() for path in files}
    try:
        _set_site(root, site.name, data)
        notes = _release(root, site.name, pilot, int(data["pilot"]["id"]),
                         robot.id if robot else None, int(data["robot"]["slot"]) if robot else None)
        _set_pilot(root, site.name, pilot, int(data["pilot"]["id"]))
        inv = Inventory.load(root)
        if robot is not None:
            inv.assign(robot.id, site.name, int(data["robot"]["slot"]))
            inv.save_robots()
            inv = Inventory.load(root)
        inv.check()
    except InventoryError as exc:
        for path, content in before.items():
            path.write_bytes(content)
        raise HubError(str(exc)) from None

    renderer = Renderer(inv, store)
    site = inv.site(site.name)
    renderer.render_pilots(site)
    summary = {
        "endpoint": str(site.endpoint),
        "pilot": pilot,
        "pilot_ip": str(A.operator_ip(site, inv.operators[pilot])),
    }
    if robot is not None:
        robot = inv.robot(robot.id)
        renderer.render_robot(robot)
        summary.update({"robot": robot.name, "robot_id": robot.id, "slot": robot.slot, "qgc": renderer.qgc_address(robot)})
    if notes:
        summary["released"] = notes
    return summary
