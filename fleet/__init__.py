"""fleet — конвейер прошивки и обслуживания роботов FLU."""
import os
from pathlib import Path

__version__ = "0.1.0"

# Корень репозитория: inventory/, templates/, base/, secrets/, build/
ROOT = Path(os.environ.get("FLEET_ROOT", Path(__file__).resolve().parents[1]))
