"""Командний рядок fleet. Запуск: `fleet <команда>` або `python -m fleet <команда>`.

Мова виводу: --lang uk|en або змінна FLEET_LANG (типово українська).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import ROOT
from . import addressing as A
from .i18n import LANGS, T, set_lang
from .inventory import Inventory, InventoryError
from .keystore import SecretError, SecretStore
from .render import Renderer, RenderError

STATUS_MARK = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}
YES = ("y", "yes", "т", "так", "д", "да")


def load() -> tuple[Inventory, SecretStore]:
    return Inventory.load(ROOT), SecretStore(ROOT)


def rel(path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# --- команди -------------------------------------------------------------------

def cmd_check(args) -> int:
    inv, _ = load()
    errors = inv.validate()
    for e in errors:
        print(f"  - {e}")
    if errors:
        print(T(f"помилок: {len(errors)}", f"errors: {len(errors)}"))
        return 1
    print(T(f"інвентар у порядку: майданчиків {len(inv.sites)}, НРК {len(inv.robots)}, операторів {len(inv.operators)}",
            f"inventory is fine: {len(inv.sites)} sites, {len(inv.robots)} UGVs, {len(inv.operators)} operators"))
    for s in inv.sites.values():
        busy = len(inv.robots_on(s.name))
        print(f"  {s.name:<12} {s.kind:<8} {T('НРК', 'UGVs')} {busy}/{A.capacity(s)}  {s.endpoint}")
    return 0


def cmd_render(args) -> int:
    inv, store = load()
    inv.check()
    r = Renderer(inv, store)
    if args.robot is not None:
        paths, notes = r.render_robot(inv.robot(args.robot)), []
    elif args.site:
        site = inv.site(args.site)
        paths, notes = (r.render_site(site) if site.managed else r.render_kit(site)), []
    else:
        paths, notes = r.render_all()
    print(T(f"зібрано файлів: {len(paths)} -> build/", f"files built: {len(paths)} -> build/"))
    for n in notes:
        print(f"  - {n}")
    return 0


def cmd_provision(args) -> int:
    from .provision import provision_robot

    inv, store = load()
    robot = inv.robot(args.id)
    notes = provision_robot(inv, store, robot, args.site, allow_new_key=args.new_key)
    site = inv.site(robot.site)
    out = rel(Renderer(inv, store).robot_dir(robot))
    sec = store.robot(robot.id)
    print(T(f"{robot.name}: майданчик {site.name}, slot {robot.slot} (MAVLink SYSID {robot.slot})",
            f"{robot.name}: site {site.name}, slot {robot.slot} (MAVLink SYSID {robot.slot})"))
    for n in notes:
        print(f"  - {n}")
    print()
    print(T(f"Конфіги: {out}/", f"Configs: {out}/"))
    print()
    print(T("На стенді:", "At the bench:"))
    print(T(f"  1. MikroTik. Netinstall з Configure script {out}/robot.rsc",
            f"  1. MikroTik. Netinstall with Configure script {out}/robot.rsc"))
    print(f"     (netinstall-cli -r -s {out}/robot.rsc -a 192.168.88.3 routeros-*.npk)")
    print(T(f"     або на роутері з RouterOS: fleet bench mikrotik --id {robot.id} --password <заводський пароль>",
            f"     or on a router with RouterOS: fleet bench mikrotik --id {robot.id} --password <factory password>"))
    print(T(f"  2. Камери. Активуйте їх паролем {sec['camera_password']} (SADP / ConfigTool),",
            f"  2. Cameras. Activate them with password {sec['camera_password']} (SADP / ConfigTool),"))
    print(T(f"     підключіть у порти камер і запустіть: fleet bench cameras --id {robot.id}",
            f"     plug them into the camera ports and run: fleet bench cameras --id {robot.id}"))
    print(T(f"  3. Польотний контролер (USB): fleet fc --id {robot.id} --connect COM5 --push",
            f"  3. Flight controller (USB): fleet fc --id {robot.id} --connect COM5 --push"))
    if site.managed:
        print(T(f"  4. Сервер майданчика: fleet deploy-server --site {site.name}  (додасть НРК)",
                f"  4. Site server: fleet deploy-server --site {site.name}  (adds the UGV)"))
    else:
        print(T(f"  4. Сервер замовника: fleet kit --site {site.name} і передайте файли їхньому адміністратору",
                f"  4. Customer server: fleet kit --site {site.name} and send the files to their administrator"))
    print(T(f"  5. Приймальний тест: fleet test --id {robot.id}", f"  5. Acceptance test: fleet test --id {robot.id}"))
    return 0


def cmd_bench_mikrotik(args) -> int:
    from .mikrotik import BENCH_HOST, MikroTik, MikroTikError, apply_full_config, find_bench_router, save_baseline, wait_for

    inv, store = load()
    robot = inv.robot(args.id)
    r = Renderer(inv, store)
    r.render_robot(robot)
    rsc = r.robot_dir(robot) / "robot.rsc"
    _, fp = r.robot_rsc(robot, inv.site(robot.site), robot.slot)
    host = args.host or find_bench_router()
    print(T(f"{robot.name}: заливаю {rel(rsc)} на {host} і скидаю роутер",
            f"{robot.name}: uploading {rel(rsc)} to {host} and resetting the router"))
    try:
        mt = MikroTik(host, args.password)
    except MikroTikError as exc:
        raise MikroTikError(T(
            f"роутер не відповідає. Перевірте: кабель від ноутбука в порту 2, 3, 4 або 5; роутер увімкнено й скинуто "
            f"до заводських; минуло 30 секунд після ввімкнення; якщо на наліпці є пароль — його введено. ({exc})",
            f"the router does not answer. Check: the laptop cable is in port 2, 3, 4 or 5; the router is on and reset "
            f"to factory settings; 30 seconds have passed since power-on; the label password is entered if there is one. ({exc})",
        )) from None
    with mt:
        apply_full_config(mt, rsc)
    print(T("роутер перезавантажується, чекаю...", "the router is rebooting, waiting..."))
    with wait_for(BENCH_HOST, store.robot(robot.id)["mikrotik_password"]) as mt:
        got = mt.fingerprint()
        if got != fp:
            print(T(f"конфіг застосувався не повністю: відбиток {got}, очікувався {fp}. Дивіться /log print на роутері",
                    f"the config was applied partially: fingerprint {got}, expected {fp}. See /log print on the router"))
            return 1
        path = save_baseline(r.robot_dir(robot), mt)
    print(T(f"готово: відбиток {fp}, еталон для перевірки дрейфу: {rel(path)}",
            f"done: fingerprint {fp}, drift reference: {rel(path)}"))
    return 0


def cmd_bench_find(args) -> int:
    from .mndp import describe, discover

    routers = discover(args.timeout)
    for router in routers:
        print(describe(router))
    if not routers:
        print(T("роутерів MikroTik не знайдено", "no MikroTik routers found"))
    return 0 if routers else 1


def cmd_bench_cameras(args) -> int:
    from .cameras import setup_cameras
    from .mikrotik import connect_robot

    inv, store = load()
    robot = inv.robot(args.id)
    with connect_robot(inv, store, robot, via="bench") as mt:
        setup_cameras(inv, store, robot, mt, log=lambda s: print(f"  {s}"))
    print(T(f"{robot.name}: камери на {A.lan_ip('cam1')} і {A.lan_ip('cam2')}",
            f"{robot.name}: cameras at {A.lan_ip('cam1')} and {A.lan_ip('cam2')}"))
    return 0


def cmd_fc(args) -> int:
    from . import fc
    from .params import diff

    inv, store = load()
    robot = inv.robot(args.id)
    expected = Renderer(inv, store).expected_params(robot)
    print(T(f"підключаюся до {args.connect}...", f"connecting to {args.connect}..."))
    m, hb = fc.connect(args.connect, baud=args.baud)
    try:
        print(T(f"автопілот SYSID {hb.get_srcSystem()}, читаю параметри...",
                f"autopilot SYSID {hb.get_srcSystem()}, reading parameters..."))
        actual, types = fc.fetch_params(m)
        mismatch = diff(expected, actual)
        if not mismatch:
            print(T(f"усі {len(expected)} параметрів еталона збігаються", f"all {len(expected)} reference parameters match"))
            return 0
        for name, exp, act in mismatch:
            now = T("немає в прошивці", "not in firmware") if act is None else f"{act:g}"
            print(f"  {name:<20} {T('зараз', 'now')} {now:<16} {T('треба', 'need')} {exp:g}")
        if not args.push:
            print(T(f"розбіжностей: {len(mismatch)}. Записати: додайте --push",
                    f"{len(mismatch)} differences. To write them add --push"))
            return 1
        if not args.yes and input(T(f"записати {len(mismatch)} параметрів? [y/N] ",
                                    f"write {len(mismatch)} parameters? [y/N] ")).strip().lower() not in YES:
            return 1
        failed, missing = [], []
        for name, exp, act in mismatch:
            if name not in types:
                missing.append(name)
                continue
            got = fc.set_param(m, name, exp, types[name])
            if got is None or abs(got - exp) > 1e-4 * max(1.0, abs(exp)):
                failed.append(name)
        if missing:
            print(T(f"немає в прошивці (з'являться після перезавантаження, напр. NET_*): {', '.join(missing)}",
                    f"not in firmware (appear after a reboot, e.g. NET_*): {', '.join(missing)}"))
        if failed:
            print(T(f"не записалися: {', '.join(failed)}. Через арбітр писати може лише власник НРК",
                    f"not written: {', '.join(failed)}. Through the arbiter only the UGV owner can write"))
        if args.reboot:
            fc.reboot(m)
            print(T("ФК перезавантажується", "the FC is rebooting"))
        else:
            print(T("частина параметрів (SERIAL*, NET_*) застосується після перезавантаження ФК: --reboot",
                    "some parameters (SERIAL*, NET_*) apply after an FC reboot: --reboot"))
        return 1 if failed or missing else 0
    finally:
        m.close()


def cmd_deploy(args) -> int:
    from .deploy import deploy_service, deploy_site

    inv, store = load()
    inv.check()
    if args.service:
        deploy_service(inv, store, allow_new_key=args.new_key)
    else:
        deploy_site(inv, store, inv.site(args.site), allow_new_key=args.new_key)
    print(T("готово", "done"))
    return 0


def cmd_kit(args) -> int:
    inv, store = load()
    site = inv.site(args.site)
    if site.managed:
        print(T(f"майданчик {site.name} — наш (managed): файли для замовника не потрібні",
                f"site {site.name} is ours (managed): no files for the customer are needed"))
        return 1
    paths = Renderer(inv, store).render_kit(site)
    print(T(f"файли підключення для адміністратора {site.customer or site.name}: {rel(paths[0].parent)}/",
            f"connection files for the administrator of {site.customer or site.name}: {rel(paths[0].parent)}/"))
    print(T("  credentials.csv передавайте окремо від решти", "  send credentials.csv separately from the rest"))
    return 0


def cmd_move(args) -> int:
    from .move import move_robot

    inv, store = load()
    inv.check()
    robot = inv.robot(args.id)
    target = inv.site(args.to)
    confirm = (lambda _prompt: "y") if args.yes else input
    old_slot = robot.slot
    slot = move_robot(inv, store, robot, target, via=args.via, rollback_min=args.rollback_min,
                      wait_s=args.wait, confirm=confirm)
    print(T(f"{robot.name} тепер на {target.name}, slot {slot}", f"{robot.name} is now on {target.name}, slot {slot}"))
    if slot != old_slot:
        conn = f"tcp:{A.server_ip(target)}:{A.arbiter_port(slot)}"
        print(T(f"  slot змінився ({old_slot} -> {slot}): оновіть SYSID у ФК, fleet fc --id {robot.id} --connect {conn} --push",
                f"  slot changed ({old_slot} -> {slot}): update SYSID in the FC, fleet fc --id {robot.id} --connect {conn} --push"))
    print(T(f"  приймальний тест: fleet test --id {robot.id}", f"  acceptance test: fleet test --id {robot.id}"))
    return 0


def cmd_hub_command(args) -> int:
    from .hub import prepare

    result = prepare(ROOT, args.pilot, args.serial)
    print(T("Вставте цю команду на сервері (під root або користувачем із sudo):",
            "Paste this command on the server (as root or a user with sudo):"), file=sys.stderr)
    print(result["command"])
    return 0


def cmd_hub_apply(args) -> int:
    from .hub import apply_response

    text = args.text if args.text else sys.stdin.read()
    summary = apply_response(ROOT, text)
    print(T(f"сервер {summary['endpoint']}, пілот {summary['pilot']} ({summary['pilot_ip']})",
            f"server {summary['endpoint']}, pilot {summary['pilot']} ({summary['pilot_ip']})"))
    if "robot" in summary:
        print(T(f"НРК {summary['robot']}: слот {summary['slot']}, QGroundControl — TCP {summary['qgc']}",
                f"UGV {summary['robot']}: slot {summary['slot']}, QGroundControl - TCP {summary['qgc']}"))
    return 0


def cmd_wg_install(args) -> int:
    from .pilot import install_pilot_tunnel

    install_pilot_tunnel(ROOT, args.site, args.pilot, dry_run=args.dry_run)
    return 0


def cmd_ui(args) -> int:
    from .ui import serve

    serve(Path(args.root).resolve() if args.root else ROOT, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_test(args) -> int:
    from .acceptance import Acceptance, save_report

    inv, store = load()
    robot = inv.robot(args.id)
    checks = Acceptance(inv, store, robot, via=args.via, mikrotik=not args.no_mikrotik).run()
    print(T(f"Приймальний тест {robot.name}", f"Acceptance test of {robot.name}"))
    for c in checks:
        print(f"  [{STATUS_MARK[c.status]}] {c.name:<18} {c.detail}")
    path = save_report(ROOT, robot, checks)
    failed = sum(c.status == "FAIL" for c in checks)
    warned = sum(c.status == "WARN" for c in checks)
    verdict = T("не пройдено", "failed") if failed else T("пройдено", "passed")
    print(T(f"{verdict}: FAIL {failed}, WARN {warned}. Звіт: {rel(path)}", f"{verdict}: FAIL {failed}, WARN {warned}. Report: {rel(path)}"))
    return 1 if failed else 0


# --- розбір аргументів ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description=T("Конвеєр прошивки та обслуговування НРК FLU",
                                                             "FLU UGV provisioning and maintenance pipeline"))
    p.add_argument("--lang", choices=LANGS, help=T("мова виводу (типово uk або FLEET_LANG)", "output language (default uk or FLEET_LANG)"))
    sub = p.add_subparsers(dest="command", required=True)
    via_choices = ["auto", "svc", "tunnel", "site", "bench"]

    sub.add_parser("check", help=T("перевірити інвентар", "validate the inventory")).set_defaults(fn=cmd_check)

    s = sub.add_parser("render", help=T("зібрати конфіги в build/", "build configs into build/"))
    g = s.add_mutually_exclusive_group()
    g.add_argument("--site")
    g.add_argument("--robot", type=int)
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("provision", help=T("зареєструвати НРК: slot, ключі, конфіги", "register a UGV: slot, keys, configs"))
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--site", help=T("майданчик (типово з robots.yaml)", "site (default from robots.yaml)"))
    s.add_argument("--new-key", action="store_true", help=T("дозволити новий ключ сервера, якщо старий втрачено",
                                                            "allow a new server key if the old one is lost"))
    s.set_defaults(fn=cmd_provision)

    bench = sub.add_parser("bench", help=T("операції на стенді", "bench operations")).add_subparsers(dest="target", required=True)
    s = bench.add_parser("mikrotik", help=T("скинути MikroTik з конфігом НРК", "reset MikroTik with the UGV config"))
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--password", default="", help=T("поточний пароль admin (заводський — на наліпці)",
                                                    "current admin password (factory one is on the label)"))
    s.add_argument("--host", help=T("адреса роутера (типово знаходиться сама, інакше 192.168.88.1)",
                                    "router address (found automatically by default, else 192.168.88.1)"))
    s.set_defaults(fn=cmd_bench_mikrotik)
    s = bench.add_parser("find", help=T("знайти роутери MikroTik у мережі (MAC, модель, IP)",
                                        "find MikroTik routers on the network (MAC, model, IP)"))
    s.add_argument("--timeout", type=float, default=3.0)
    s.set_defaults(fn=cmd_bench_find)
    s = bench.add_parser("cameras", help=T("перенести камери із заводської IP на адреси НРК",
                                           "move the cameras from the factory IP to the UGV addresses"))
    s.add_argument("--id", type=int, required=True)
    s.set_defaults(fn=cmd_bench_cameras)

    s = sub.add_parser("fc", help=T("порівняти й записати параметри польотного контролера",
                                    "compare and write flight controller parameters"))
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--connect", required=True, help=T("COM5 (USB) або tcp:<сервер>:<порт арбітра>",
                                                      "COM5 (USB) or tcp:<server>:<arbiter port>"))
    s.add_argument("--baud", type=int, default=115200)
    s.add_argument("--push", action="store_true", help=T("записати параметри, що відрізняються", "write the differing parameters"))
    s.add_argument("--yes", action="store_true", help=T("не питати підтвердження", "do not ask for confirmation"))
    s.add_argument("--reboot", action="store_true", help=T("перезавантажити ФК після запису", "reboot the FC after writing"))
    s.set_defaults(fn=cmd_fc)

    s = sub.add_parser("deploy-server", help=T("розгорнути або оновити сервер майданчика / сервісного тунелю",
                                               "deploy or update a site server / the service tunnel"))
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--site")
    g.add_argument("--service", action="store_true")
    s.add_argument("--new-key", action="store_true", help=T("дозволити новий ключ сервера, якщо старий втрачено",
                                                            "allow a new server key if the old one is lost"))
    s.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("kit", help=T("файли підключення для сервера замовника (external)",
                                     "connection files for a customer server (external)"))
    s.add_argument("--site", required=True)
    s.set_defaults(fn=cmd_kit)

    s = sub.add_parser("move", help=T("перенести НРК на інший майданчик", "move a UGV to another site"))
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--via", default="auto", choices=via_choices)
    s.add_argument("--rollback-min", type=int, default=10, help=T("автовідкат, якщо переїзд не підтверджено",
                                                                  "auto-rollback if the move is not confirmed"))
    s.add_argument("--wait", type=int, default=180, help=T("скільки секунд чекати новий тунель", "seconds to wait for the new tunnel"))
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_move)

    s = sub.add_parser("test", help=T("приймальний тест НРК", "UGV acceptance test"))
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--via", default="auto", choices=via_choices)
    s.add_argument("--no-mikrotik", action="store_true")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("hub-command", help=T("команда для сервера: WireGuard, пілот і НРК",
                                             "command for the server: WireGuard, pilot and UGV"))
    s.add_argument("--pilot", required=True, help=T("ім'я пілота цього ноутбука (a-z, 0-9, -)", "pilot name of this laptop (a-z, 0-9, -)"))
    s.add_argument("--serial", help=T("заводський номер НРК", "UGV serial number"))
    s.set_defaults(fn=cmd_hub_command)

    s = sub.add_parser("hub-apply", help=T("застосувати відповідь сервера (рядок FLU1:...:END)",
                                           "apply the server response (the FLU1:...:END line)"))
    s.add_argument("--text", help=T("рядок відповіді; без нього читається зі stdin", "the response line; read from stdin if omitted"))
    s.set_defaults(fn=cmd_hub_apply)

    s = sub.add_parser("wg-install", help=T("поставити тунель пілота у WireGuard", "install the pilot tunnel into WireGuard"))
    s.add_argument("--pilot", required=True)
    s.add_argument("--site", default="main")
    s.add_argument("--dry-run", action="store_true", help=T("лише показати, що буде зроблено", "only show what would be done"))
    s.set_defaults(fn=cmd_wg_install)

    s = sub.add_parser("ui", help=T("локальний веб-інтерфейс (http://127.0.0.1:8765)", "local web UI (http://127.0.0.1:8765)"))
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help=T("не відкривати браузер", "do not open a browser"))
    s.add_argument("--root", help=T("інша копія репозиторію (типово ця)", "another copy of the repository (default this one)"))
    s.set_defaults(fn=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", line_buffering=True)
    argv = sys.argv[1:] if argv is None else argv
    # Мову треба знати ще до побудови парсера: від неї залежать тексти довідки
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", choices=LANGS)
    lang = pre.parse_known_args(argv)[0].lang
    if lang:
        os.environ["FLEET_LANG"] = lang  # і для дочірніх процесів (fleet ui)
    set_lang(lang or os.environ.get("FLEET_LANG"))

    args = build_parser().parse_args(argv)
    from .cameras import CameraError
    from .hub import HubError
    from .pilot import TunnelError
    from .deploy import DeployError
    from .fc import FcError
    from .mikrotik import MikroTikError
    from .move import MoveError

    known = (InventoryError, SecretError, RenderError, MikroTikError, CameraError, FcError, DeployError, MoveError, HubError, TunnelError)
    try:
        code = args.fn(args)
    except known as exc:
        print(T(f"помилка: {exc}", f"error: {exc}"), file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
