"""Переезд робота на другую площадку без выезда на место.

1. Новая площадка получает пира робота (managed — деплой, external — пакет заказчику).
2. На MikroTik рядом со старым туннелем поднимается новый (pending) с автооткатом.
3. Новый туннель поймал handshake — подтверждаем переезд, старый туннель удаляется.
4. Handshake нет — откатываем сразу, робот остаётся на старой площадке.
"""
from __future__ import annotations

import time

from .deploy import deploy_site
from .inventory import Inventory, InventoryError, Robot, Site
from .keystore import SecretStore
from .mikrotik import connect_robot
from .render import Renderer

HANDSHAKE_FRESH_S = 120


class MoveError(Exception):
    pass


def move_robot(inv: Inventory, store: SecretStore, robot: Robot, target: Site, *, via: str = "auto",
               rollback_min: int = 10, wait_s: int = 180, confirm=input, log=print) -> int:
    """Перевезти робота на площадку target. Возвращает новый slot."""
    if not robot.provisioned:
        raise InventoryError(f"{robot.name} ещё не прошит: fleet provision --id {robot.id}")
    if target.name == robot.site:
        raise InventoryError(f"{robot.name} уже на площадке {target.name}")
    if target.managed and not store.has_site(target.name):
        raise MoveError(f"сервер площадки {target.name} ещё не развёрнут: fleet deploy-server --site {target.name}")
    old_site, old_slot = inv.site(robot.site), robot.slot
    new_slot = inv.free_slot(target.name, prefer=robot.slot)
    renderer = Renderer(inv, store)
    add_rsc, commit_rsc = renderer.render_move(robot, target, new_slot, rollback_min)

    # 1. Пир робота на новой площадке. Для сборки конфига робот временно числится там.
    inv.assign(robot.id, target.name, new_slot)
    try:
        if target.managed:
            deploy_site(inv, store, target, log=log)
        else:
            kit = renderer.render_kit(target)
            log(f"Передайте администратору {target.customer or target.name} файл {kit[1]} "
                f"(пир {robot.name}) и дождитесь, пока он добавит его на сервер.")
            if confirm("Пир добавлен на сервере заказчика? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
                raise MoveError("переезд отменён")
    finally:
        inv.assign(robot.id, old_site.name, old_slot)  # до подтверждения робот на старой площадке

    # 2-4. Новый туннель рядом со старым, ждём handshake, подтверждаем или откатываем
    pending_tag = f"fleet-site:{target.name} pending"
    with connect_robot(inv, store, robot, via=via) as mt:
        log(f"{robot.name}: добавляю площадку {target.name} (автооткат через {rollback_min} мин)")
        mt.import_file(add_rsc)
        deadline = time.monotonic() + wait_s
        while True:
            age = mt.last_handshake(pending_tag)
            if age is not None and age < HANDSHAKE_FRESH_S:
                break
            if time.monotonic() > deadline:
                mt.run(f'/system script run [find name="fleet-rollback-{target.name}"]', check=False)
                raise MoveError(
                    f"новый туннель не поднялся за {wait_s} с — откатил, {robot.name} остался на {old_site.name}. "
                    f"Проверьте, что пир робота есть на сервере {target.name} и порт {target.endpoint} открыт"
                )
            time.sleep(5)
        log(f"{robot.name}: туннель {target.name} поднялся, подтверждаю переезд")
        mt.import_file(commit_rsc, background=True)

    inv.assign(robot.id, target.name, new_slot)
    inv.save_robots()
    renderer.render_robot(robot)

    # Старая площадка: убрать пира робота
    if old_site.managed and store.has_site(old_site.name):
        deploy_site(inv, store, old_site, log=log)
    elif not old_site.managed:
        renderer.render_kit(old_site)
        log(f"Сообщите администратору {old_site.customer or old_site.name}: пира {robot.name} можно удалить.")
    return new_slot
