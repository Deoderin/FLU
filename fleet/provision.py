"""Регистрация нового робота: площадка, slot, ключи, конфиги."""
from __future__ import annotations

from .deploy import ensure_service_key, ensure_site_key
from .inventory import Inventory, InventoryError, Robot
from .keystore import SecretStore
from .render import Renderer


def provision_robot(inv: Inventory, store: SecretStore, robot: Robot, site_name: str | None = None,
                    allow_new_key: bool = False) -> list[str]:
    """Выдать роботу slot и ключи и собрать его конфиги. Повторный запуск безопасен:
    slot и ключи сохраняются, конфиги пересобираются. Возвращает заметки для человека."""
    inv.check()
    site_name = site_name or robot.site
    if not site_name:
        raise InventoryError(f"{robot.name}: укажите площадку (--site или site в robots.yaml)")
    site = inv.site(site_name)
    if robot.provisioned and robot.site != site_name:
        raise InventoryError(f"{robot.name} уже на площадке {robot.site}: для смены площадки — fleet move")
    if robot.service_tunnel and inv.service is None:
        raise InventoryError(f"{robot.name}: service_tunnel включён, но раздел service в sites.yaml не задан")

    notes = []
    if site.managed and ensure_site_key(inv, store, site, allow_new_key, robot.id):
        notes.append(f"создан ключ сервера площадки {site.name}")
    if robot.service_tunnel and ensure_service_key(inv, store, allow_new_key, robot.id):
        notes.append("создан ключ сервера сервисного туннеля")

    slot = robot.slot if robot.provisioned else inv.free_slot(site_name)
    store.robot(robot.id, create=True)
    inv.assign(robot.id, site_name, slot)
    inv.check()
    inv.save_robots()
    Renderer(inv, store).render_robot(robot)
    return notes
