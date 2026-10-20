"""Командная строка fleet. Запуск: `fleet <команда>` или `python -m fleet <команда>`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ROOT
from . import addressing as A
from .inventory import Inventory, InventoryError
from .keystore import SecretError, SecretStore
from .render import Renderer, RenderError

STATUS_MARK = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}


def load() -> tuple[Inventory, SecretStore]:
    return Inventory.load(ROOT), SecretStore(ROOT)


def rel(path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# --- команды -------------------------------------------------------------------

def cmd_check(args) -> int:
    inv, _ = load()
    errors = inv.validate()
    for e in errors:
        print(f"  - {e}")
    if errors:
        print(f"ошибок: {len(errors)}")
        return 1
    print(f"inventory в порядке: площадок {len(inv.sites)}, роботов {len(inv.robots)}, операторов {len(inv.operators)}")
    for s in inv.sites.values():
        busy = len(inv.robots_on(s.name))
        print(f"  {s.name:<12} {s.kind:<8} роботов {busy}/{A.capacity(s)}  {s.endpoint}")
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
    print(f"собрано файлов: {len(paths)} -> build/")
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
    print(f"{robot.name}: площадка {site.name}, slot {robot.slot} (MAVLink SYSID {robot.slot})")
    for n in notes:
        print(f"  - {n}")
    print(f"""
Конфиги: {out}/

На стенде:
  1. MikroTik. Netinstall с Configure script {out}/robot.rsc
     (netinstall-cli -r -s {out}/robot.rsc -a 192.168.88.3 routeros-*.npk)
     или на роутере с RouterOS: fleet bench mikrotik --id {robot.id} --password <заводской пароль>
  2. Камеры. Активируйте их паролем {sec['camera_password']} (SADP / ConfigTool),
     подключите в порты камер и запустите: fleet bench cameras --id {robot.id}
  3. Полётный контроллер (USB): fleet fc --id {robot.id} --connect COM5 --push""")
    if site.managed:
        print(f"  4. Сервер площадки: fleet deploy-server --site {site.name}  (добавит робота)")
    else:
        print(f"  4. Площадка заказчика: fleet kit --site {site.name} и передайте пакет их администратору")
    print(f"  5. Приёмка: fleet test --id {robot.id}")
    return 0


def cmd_bench_mikrotik(args) -> int:
    from .mikrotik import BENCH_HOST, MikroTik, apply_full_config, save_baseline, wait_for

    inv, store = load()
    robot = inv.robot(args.id)
    r = Renderer(inv, store)
    r.render_robot(robot)
    rsc = r.robot_dir(robot) / "robot.rsc"
    _, fp = r.robot_rsc(robot, inv.site(robot.site), robot.slot)
    host = args.host or BENCH_HOST
    print(f"{robot.name}: заливаю {rel(rsc)} на {host} и сбрасываю роутер")
    with MikroTik(host, args.password) as mt:
        apply_full_config(mt, rsc)
    print("роутер перезагружается, жду...")
    with wait_for(BENCH_HOST, store.robot(robot.id)["mikrotik_password"]) as mt:
        got = mt.fingerprint()
        if got != fp:
            print(f"конфиг применился не полностью: отпечаток {got}, ожидался {fp}. Смотрите /log print на роутере")
            return 1
        path = save_baseline(r.robot_dir(robot), mt)
    print(f"готово: отпечаток {fp}, эталон для проверки дрейфа: {rel(path)}")
    return 0


def cmd_bench_cameras(args) -> int:
    from .cameras import setup_cameras
    from .mikrotik import connect_robot

    inv, store = load()
    robot = inv.robot(args.id)
    with connect_robot(inv, store, robot, via="bench") as mt:
        setup_cameras(inv, store, robot, mt, log=lambda s: print(f"  {s}"))
    print(f"{robot.name}: камеры на {A.lan_ip('cam1')} и {A.lan_ip('cam2')}")
    return 0


def cmd_fc(args) -> int:
    from . import fc
    from .params import diff

    inv, store = load()
    robot = inv.robot(args.id)
    expected = Renderer(inv, store).expected_params(robot)
    print(f"подключаюсь к {args.connect}...")
    m, hb = fc.connect(args.connect, baud=args.baud)
    try:
        print(f"автопилот SYSID {hb.get_srcSystem()}, читаю параметры...")
        actual, types = fc.fetch_params(m)
        mismatch = diff(expected, actual)
        if not mismatch:
            print(f"все {len(expected)} параметров эталона совпадают")
            return 0
        for name, exp, act in mismatch:
            print(f"  {name:<20} сейчас {'нет в прошивке' if act is None else f'{act:g}':<16} нужно {exp:g}")
        if not args.push:
            print(f"расхождений: {len(mismatch)}. Записать: добавьте --push")
            return 1
        if not args.yes and input(f"записать {len(mismatch)} параметров? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
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
            print(f"нет в прошивке (появятся после перезагрузки, например NET_*): {', '.join(missing)}")
        if failed:
            print(f"не записались: {', '.join(failed)}. Через арбитр писать может только владелец робота")
        if args.reboot:
            fc.reboot(m)
            print("ФК перезагружается")
        else:
            print("часть параметров (SERIAL*, NET_*) применится после перезагрузки ФК: --reboot")
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
    print("готово")
    return 0


def cmd_kit(args) -> int:
    inv, store = load()
    site = inv.site(args.site)
    if site.managed:
        print(f"площадка {site.name} — наша (managed): пакет для заказчика не нужен")
        return 1
    paths = Renderer(inv, store).render_kit(site)
    print(f"пакет для администратора {site.customer or site.name}: {rel(paths[0].parent)}/")
    print("  credentials.csv передавайте отдельно от остального")
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
    print(f"{robot.name} теперь на {target.name}, slot {slot}")
    if slot != old_slot:
        print(f"  slot сменился ({old_slot} -> {slot}): обновите SYSID в ФК, "
              f"fleet fc --id {robot.id} --connect tcp:{A.server_ip(target)}:{A.arbiter_port(slot)} --push")
    print(f"  приёмка: fleet test --id {robot.id}")
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
    print(f"Приёмочный тест {robot.name}")
    for c in checks:
        print(f"  [{STATUS_MARK[c.status]}] {c.name:<18} {c.detail}")
    path = save_report(ROOT, robot, checks)
    failed = sum(c.status == "FAIL" for c in checks)
    print(f"{'не пройден' if failed else 'пройден'}: FAIL {failed}, WARN {sum(c.status == 'WARN' for c in checks)}. Отчёт: {rel(path)}")
    return 1 if failed else 0


# --- разбор аргументов ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description="Конвейер прошивки и обслуживания роботов FLU")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="проверить inventory").set_defaults(fn=cmd_check)

    s = sub.add_parser("render", help="собрать конфиги в build/")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--site")
    g.add_argument("--robot", type=int)
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("provision", help="зарегистрировать робота: slot, ключи, конфиги")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--site", help="площадка (по умолчанию из robots.yaml)")
    s.add_argument("--new-key", action="store_true", help="разрешить новый ключ сервера, если старый потерян")
    s.set_defaults(fn=cmd_provision)

    bench = sub.add_parser("bench", help="операции на стенде").add_subparsers(dest="target", required=True)
    s = bench.add_parser("mikrotik", help="сбросить MikroTik с конфигом робота")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--password", default="", help="текущий пароль admin (заводской — на наклейке)")
    s.add_argument("--host", help=f"адрес роутера (по умолчанию {A.lan_ip('router')})")
    s.set_defaults(fn=cmd_bench_mikrotik)
    s = bench.add_parser("cameras", help="перевести камеры с заводского IP на адреса робота")
    s.add_argument("--id", type=int, required=True)
    s.set_defaults(fn=cmd_bench_cameras)

    s = sub.add_parser("fc", help="сравнить и залить параметры полётного контроллера")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--connect", required=True, help="COM5 (USB) или tcp:<сервер>:<порт арбитра>")
    s.add_argument("--baud", type=int, default=115200)
    s.add_argument("--push", action="store_true", help="записать отличающиеся параметры")
    s.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    s.add_argument("--reboot", action="store_true", help="перезагрузить ФК после записи")
    s.set_defaults(fn=cmd_fc)

    s = sub.add_parser("deploy-server", help="развернуть или обновить сервер площадки / сервисного туннеля")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--site")
    g.add_argument("--service", action="store_true")
    s.add_argument("--new-key", action="store_true", help="разрешить новый ключ сервера, если старый потерян")
    s.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("kit", help="интеграционный пакет для сервера заказчика (external)")
    s.add_argument("--site", required=True)
    s.set_defaults(fn=cmd_kit)

    s = sub.add_parser("move", help="перевезти робота на другую площадку")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--via", default="auto", choices=["auto", "svc", "site", "bench"])
    s.add_argument("--rollback-min", type=int, default=10, help="автооткат, если переезд не подтверждён")
    s.add_argument("--wait", type=int, default=180, help="сколько секунд ждать новый туннель")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_move)

    s = sub.add_parser("test", help="приёмочный тест робота")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--via", default="auto", choices=["auto", "svc", "site", "bench"])
    s.add_argument("--no-mikrotik", action="store_true")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("ui", help="локальный веб-интерфейс (http://127.0.0.1:8765)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    s.add_argument("--root", help="другая копия репозитория (по умолчанию эта)")
    s.set_defaults(fn=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", line_buffering=True)
    args = build_parser().parse_args(argv)
    from .cameras import CameraError
    from .deploy import DeployError
    from .fc import FcError
    from .mikrotik import MikroTikError
    from .move import MoveError

    known = (InventoryError, SecretError, RenderError, MikroTikError, CameraError, FcError, DeployError, MoveError)
    try:
        code = args.fn(args)
    except known as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
