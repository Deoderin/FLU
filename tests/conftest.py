import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO / "server" / "arbiter"))  # арбітр — окремий застосунок, імпортуємо як є

from fleet.i18n import set_lang  # noqa: E402
from fleet.inventory import Inventory  # noqa: E402
from fleet.keystore import SecretStore  # noqa: E402


@pytest.fixture(autouse=True)
def ukrainian():
    """Тести за замовчуванням перевіряють українські повідомлення."""
    set_lang("uk")
    yield
    set_lang("uk")


@pytest.fixture
def root(tmp_path) -> Path:
    """Копія репозиторію з тестовим інвентарем, без build/ і secrets/: справжні ключі не чіпаємо."""
    for name in ("base", "templates", "server"):
        shutil.copytree(REPO / name, tmp_path / name)
    shutil.copytree(FIXTURES / "inventory", tmp_path / "inventory")
    return tmp_path


@pytest.fixture
def inv(root) -> Inventory:
    return Inventory.load(root)


@pytest.fixture
def store(root) -> SecretStore:
    return SecretStore(root)


@pytest.fixture
def provisioned(root, inv, store):
    """Усі тестові НРК зареєстровано."""
    from fleet.provision import provision_robot

    for robot_id in sorted(inv.robots):
        provision_robot(inv, store, inv.robot(robot_id))
    return inv
