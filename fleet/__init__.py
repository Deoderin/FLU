"""fleet — конвеєр прошивки та обслуговування НРК. Для людей програма зветься UGV Quick Setup."""
import os
from pathlib import Path

__version__ = "0.2.0"
APP_NAME = "UGV Quick Setup"  # ярлик, вікно, майстер

# Корінь репозиторію: inventory/, templates/, base/, secrets/, build/
ROOT = Path(os.environ.get("FLEET_ROOT", Path(__file__).resolve().parents[1]))
