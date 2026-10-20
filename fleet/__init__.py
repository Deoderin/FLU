"""fleet — конвеєр прошивки та обслуговування НРК FLU."""
import os
from pathlib import Path

__version__ = "0.2.0"

# Корінь репозиторію: inventory/, templates/, base/, secrets/, build/
ROOT = Path(os.environ.get("FLEET_ROOT", Path(__file__).resolve().parents[1]))
