"""Переїзд НРК на інший майданчик без виїзду на місце.

1. Новий майданчик отримує пір НРК (managed — розгортання, external — файли замовнику).
2. На MikroTik поруч зі старим тунелем піднімається новий (pending) з автовідкатом.
3. Новий тунель зловив handshake — підтверджуємо переїзд, старий тунель видаляється.
4. Handshake немає — одразу відкочуємо, НРК лишається на старому майданчику.
"""
from __future__ import annotations

import time

from .deploy import deploy_site
from .i18n import T
from .inventory import Inventory, InventoryError, Robot, Site
from .keystore import SecretStore
from .mikrotik import connect_robot
from .render import Renderer

HANDSHAKE_FRESH_S = 120
YES = ("y", "yes", "т", "так", "д", "да")


class MoveError(Exception):
    pass


def move_robot(inv: Inventory, store: SecretStore, robot: Robot, target: Site, *, via: str = "auto",
               rollback_min: int = 10, wait_s: int = 180, confirm=input, log=print) -> int:
    """Перенести НРК на майданчик target. Повертає новий slot."""
    if not robot.provisioned:
        raise InventoryError(T(f"{robot.name} ще не зареєстровано: fleet provision --id {robot.id}",
                               f"{robot.name} is not registered yet: fleet provision --id {robot.id}"))
    if target.name == robot.site:
        raise InventoryError(T(f"{robot.name} вже на майданчику {target.name}", f"{robot.name} is already on site {target.name}"))
    if target.managed and not store.has_site(target.name):
        raise MoveError(T(f"сервер майданчика {target.name} ще не розгорнуто: fleet deploy-server --site {target.name}",
                          f"the server of site {target.name} is not deployed yet: fleet deploy-server --site {target.name}"))
    old_site, old_slot = inv.site(robot.site), robot.slot
    new_slot = inv.free_slot(target.name, prefer=robot.slot)
    renderer = Renderer(inv, store)
    add_rsc, commit_rsc = renderer.render_move(robot, target, new_slot, rollback_min)

    # 1. Пір НРК на новому майданчику. Для збирання конфігу НРК тимчасово числиться там.
    inv.assign(robot.id, target.name, new_slot)
    try:
        if target.managed:
            deploy_site(inv, store, target, log=log)
        else:
            kit = renderer.render_kit(target)
            customer = target.customer or target.name
            log(T(f"Передайте адміністратору {customer} файл {kit[1]} (пір {robot.name}) і дочекайтеся, поки він додасть його на сервер.",
                  f"Send {kit[1]} (peer {robot.name}) to the administrator of {customer} and wait until it is added to the server."))
            answer = confirm(T("Пір додано на сервері замовника? [y/N] ", "Has the peer been added on the customer's server? [y/N] "))
            if answer.strip().lower() not in YES:
                raise MoveError(T("переїзд скасовано", "move cancelled"))
    finally:
        inv.assign(robot.id, old_site.name, old_slot)  # до підтвердження НРК на старому майданчику

    # 2-4. Новий тунель поруч зі старим, чекаємо handshake, підтверджуємо або відкочуємо
    pending_tag = f"fleet-site:{target.name} pending"
    with connect_robot(inv, store, robot, via=via) as mt:
        log(T(f"{robot.name}: додаю майданчик {target.name} (автовідкат через {rollback_min} хв)",
              f"{robot.name}: adding site {target.name} (auto-rollback in {rollback_min} min)"))
        mt.import_file(add_rsc)
        deadline = time.monotonic() + wait_s
        while True:
            age = mt.last_handshake(pending_tag)
            if age is not None and age < HANDSHAKE_FRESH_S:
                break
            if time.monotonic() > deadline:
                mt.run(f'/system script run [find name="fleet-rollback-{target.name}"]', check=False)
                raise MoveError(T(
                    f"новий тунель не піднявся за {wait_s} с — відкотив, {robot.name} лишився на {old_site.name}. "
                    f"Перевірте, що пір НРК є на сервері {target.name} і порт {target.endpoint} відкритий",
                    f"the new tunnel did not come up within {wait_s} s - rolled back, {robot.name} stays on {old_site.name}. "
                    f"Check that the UGV peer exists on the {target.name} server and port {target.endpoint} is open",
                ))
            time.sleep(5)
        log(T(f"{robot.name}: тунель {target.name} піднявся, підтверджую переїзд",
              f"{robot.name}: tunnel {target.name} is up, confirming the move"))
        mt.import_file(commit_rsc, background=True)

    inv.assign(robot.id, target.name, new_slot)
    inv.save_robots()
    renderer.render_robot(robot)

    # Старий майданчик: прибрати пір НРК
    if old_site.managed and store.has_site(old_site.name):
        deploy_site(inv, store, old_site, log=log)
    elif not old_site.managed:
        renderer.render_kit(old_site)
        customer = old_site.customer or old_site.name
        log(T(f"Повідомте адміністратору {customer}: пір {robot.name} можна видалити.",
              f"Tell the administrator of {customer} that peer {robot.name} can be removed."))
    return new_slot
