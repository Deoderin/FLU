"""Реєстрація нового НРК: майданчик, slot, ключі, конфіги."""
from __future__ import annotations

from .deploy import ensure_service_key, ensure_site_key
from .i18n import T
from .inventory import Inventory, InventoryError, Robot
from .keystore import SecretStore
from .render import Renderer


def provision_robot(inv: Inventory, store: SecretStore, robot: Robot, site_name: str | None = None,
                    allow_new_key: bool = False) -> list[str]:
    """Видати НРК slot і ключі та зібрати його конфіги. Повторний запуск безпечний:
    slot і ключі зберігаються, конфіги перезбираються. Повертає нотатки для людини."""
    inv.check()
    site_name = site_name or robot.site
    if not site_name:
        raise InventoryError(T(f"{robot.name}: вкажіть майданчик (--site або site у robots.yaml)",
                               f"{robot.name}: specify a site (--site or site in robots.yaml)"))
    site = inv.site(site_name)
    if robot.provisioned and robot.site != site_name:
        raise InventoryError(T(f"{robot.name} вже на майданчику {robot.site}: для зміни майданчика — fleet move",
                               f"{robot.name} is already on site {robot.site}: use fleet move to change it"))
    if robot.service_tunnel and inv.service is None:
        raise InventoryError(T(f"{robot.name}: service_tunnel увімкнено, але розділ service у sites.yaml не задано",
                               f"{robot.name}: service_tunnel is on but the service section is missing in sites.yaml"))

    notes = []
    if site.managed and ensure_site_key(inv, store, site, allow_new_key, robot.id):
        notes.append(T(f"створено ключ сервера майданчика {site.name}", f"created the server key of site {site.name}"))
    if robot.service_tunnel and ensure_service_key(inv, store, allow_new_key, robot.id):
        notes.append(T("створено ключ сервера сервісного тунелю", "created the service tunnel server key"))

    slot = robot.slot if robot.provisioned else inv.free_slot(site_name)
    store.robot(robot.id, create=True)
    inv.assign(robot.id, site_name, slot)
    inv.check()
    inv.save_robots()
    Renderer(inv, store).render_robot(robot)
    return notes
