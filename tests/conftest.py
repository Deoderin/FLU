import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server" / "arbiter"))  # арбитр — отдельное приложение, импортируем как есть

from fleet.inventory import Inventory  # noqa: E402
from fleet.keystore import SecretStore  # noqa: E402


@pytest.fixture
def root(tmp_path) -> Path:
    """Копия репозитория без build/ и secrets/: тесты не трогают настоящие ключи."""
    for name in ("inventory", "base", "templates", "server"):
        shutil.copytree(REPO / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def inv(root) -> Inventory:
    return Inventory.load(root)


@pytest.fixture
def store(root) -> SecretStore:
    return SecretStore(root)


@pytest.fixture
def provisioned(root, inv, store):
    """Все роботы примера прошиты."""
    from fleet.provision import provision_robot

    for robot_id in sorted(inv.robots):
        provision_robot(inv, store, inv.robot(robot_id))
    return inv
