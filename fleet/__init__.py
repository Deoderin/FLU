"""fleet — конвеєр прошивки та обслуговування НРК. Для людей програма зветься UGV Quick Setup."""
import os
import sys
from pathlib import Path

# Єдиний номер версії: pyproject.toml, заголовок вікна, журнали, назва архіву й тег релізу (v0.3.0)
__version__ = "0.3.2"
APP_NAME = "UGV Quick Setup"  # ярлик, вікно, майстер
# Допоміжні команди (sc.exe, powershell) без власного чорного вікна: у вікні майстра немає консолі,
# і кожна така команда інакше блимала б окремим вікном (CREATE_NO_WINDOW)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Можливості, ще не перевірені в полі: у зібраній програмі (.exe) вимкнені, у версії з репозиторію —
# увімкнені, щоб їх перевірити. Перевірили — приберіть зі списку. Увімкнути в зібраній: FLEET_EXPERIMENTAL=1.
#   give — передати НРК іншому пілоту (картка 7, hub-admin --give і повторне додавання НРК з іншим пілотом)
EXPERIMENTAL = {"give"}


def feature(name: str) -> bool:
    if name not in EXPERIMENTAL:
        return True
    return not getattr(sys, "frozen", False) or os.environ.get("FLEET_EXPERIMENTAL") == "1"


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
