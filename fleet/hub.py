"""Сервер «hub» з однієї команди: пілот вставляє її на сервері й копіює відповідь назад.

1. prepare()  — ключі пілота й НРК створюються на ноутбуці; в команду йдуть лише відкриті ключі.
2. command()  — одна команда для сервера: скрипт server/hub/flu-hub.sh, стиснутий у base64.
               Скрипт сам ставить WireGuard, якщо його немає, і додає пілота й НРК.
3. apply_response() — рядок FLU1:...:END із сервера -> інвентар (адреса й ключ сервера,
               номер пілота, слот НРК) і конфіги: тунель пілота й конфіг роутера.
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
from .i18n import T
from .inventory import NAME_RE, Inventory, InventoryError, _yaml, is_wg_key
from .keystore import SecretStore, wg_pubkey
from .render import Renderer

SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
RESPONSE_RE = re.compile(r"FLU1:([A-Za-z0-9+/=\s]+?):END")
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


# --- команда для сервера ---------------------------------------------------------

def validate(pilot: str, serial: str | None) -> None:
    if not NAME_RE.match(pilot or ""):
        raise HubError(T("ім'я пілота: латиниця a-z, цифри й '-', до 20 символів (наприклад, ivan)",
                         "pilot name: latin a-z, digits and '-', up to 20 characters (e.g. ivan)"))
    if serial is not None and not SERIAL_RE.match(serial):
        raise HubError(T("номер НРК: латиниця, цифри, '.', '_' і '-', до 40 символів",
                         "UGV number: latin letters, digits, '.', '_' and '-', up to 40 characters"))


def prepare(root: Path, pilot: str, serial: str | None) -> dict:
    """Ключі пілота й НРК на ноутбуці + НРК в robots.yaml. Повторний виклик нічого не ламає."""
    validate(pilot, serial)
    inv = Inventory.load(root)
    site = hub_site(inv)
    store = SecretStore(root)
    # Без ключа тут створився б новий, і сервер замінив би ним старий: пілот або НРК на іншому ноутбуці втратив би зв'язок
    if pilot in inv.operators and not (store.dir / "operators" / f"{pilot}.json").exists():
        raise HubError(T(f"пілот {pilot} уже є, але його ключ на іншому ноутбуці: виберіть інше ім'я або перенесіть сюди теку secrets/",
                         f"pilot {pilot} already exists, but the key is on another laptop: choose another name or copy the secrets/ folder here"))
    known = next((r for r in inv.robots.values() if r.serial == serial), None)
    if known and known.provisioned and not (store.dir / "robots" / f"{known.id}.json").exists():
        raise HubError(T(f"НРК {serial} уже зареєстровано на іншому ноутбуці: додавайте його звідти або перенесіть сюди теку secrets/",
                         f"UGV {serial} is already registered on another laptop: add it from there or copy the secrets/ folder here"))
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
    script = (root / SCRIPT).read_bytes().replace(b"\r\n", b"\n")
    payload = base64.b64encode(gzip.compress(script, mtime=0)).decode()
    quoted = " ".join(f"'{a}'" for a in args)  # у значеннях немає апострофів: їх перевірено вище
    return f"echo {payload} | base64 -d | gunzip > /tmp/flu-hub.sh && bash /tmp/flu-hub.sh {quoted}"


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

    _set_site(root, site.name, data)
    _set_pilot(root, site.name, pilot, int(data["pilot"]["id"]))
    inv = Inventory.load(root)
    if robot is not None:
        inv.assign(robot.id, site.name, int(data["robot"]["slot"]))
        inv.save_robots()
        inv = Inventory.load(root)
    try:
        inv.check()
    except InventoryError as exc:
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
    return summary
