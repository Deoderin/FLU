"""fleet — конвеєр прошивки та обслуговування НРК. Для людей програма зветься UGV Quick Setup."""
import os
import sys
from pathlib import Path

__version__ = "0.2.0"
APP_NAME = "UGV Quick Setup"  # ярлик, вікно, майстер
# Допоміжні команди (sc.exe, powershell) без власного чорного вікна: у вікні майстра немає консолі,
# і кожна така команда інакше блимала б окремим вікном (CREATE_NO_WINDOW)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _root() -> Path:
    """Корінь із даними: inventory/, templates/, base/, secrets/, build/.

    Зібрана програма (.exe) тримає їх поруч із собою, бо код лежить у _internal; інакше — корінь репозиторію.
    """
    if os.environ.get("FLEET_ROOT"):
        return Path(os.environ["FLEET_ROOT"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = _root()
