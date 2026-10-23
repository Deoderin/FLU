"""Збірка UGV Quick Setup в окрему програму: dist/UGV Quick Setup/ і zip поруч.

Python на ноутбуці, куди її передають, не потрібен. У збірку потрапляють лише шаблони:
inventory/ і secrets/ цього ноутбука туди не копіюються.
Запуск: build-exe.cmd (або python packaging/build.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

from fleet import APP_NAME, __version__  # noqa: E402
from make_icon import make_icon  # noqa: E402

DIST = ROOT / "dist"
DATA_DIRS = ["base", "templates", "server", "inventory.example"]
DATA_FILES = ["README.md"]
NEVER = {"inventory", "secrets", "build"}  # свої дані ноутбука в збірку не кладемо
FIRST_RUN = """UGV Quick Setup {version}

1. Покладіть цю теку туди, де шлях лише латиницею й можна писати, наприклад C:\\UGV Quick Setup
   (не в Program Files: програма зберігає поруч із собою inventory\\, secrets\\ і build\\).
2. Встановіть WireGuard для Windows: https://www.wireguard.com/install/
3. Запустіть «UGV Quick Setup.exe». Ярлик на робочому столі з'явиться сам.

Після першого НРК збережіть теки inventory\\ і secrets\\ у надійне місце: без них не обслужити ні сервер, ні НРК.
"""


def main() -> int:
    icon = make_icon(ROOT / "packaging" / "app.ico")  # той самий значок, що в ярлика з .venv
    env = {**os.environ, "UGV_ICON": str(icon)}
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
           "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
           str(ROOT / "packaging" / "ugv-quick-setup.spec")]
    print("PyInstaller...", flush=True)
    if subprocess.run(cmd, cwd=ROOT, env=env).returncode:
        return 1
    out = DIST / APP_NAME
    for name in DATA_DIRS:
        shutil.copytree(ROOT / name, out / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    for name in DATA_FILES:
        shutil.copy2(ROOT / name, out / name)
    (out / "ПРОЧИТАЙ.txt").write_text(FIRST_RUN.format(version=__version__), encoding="utf-8-sig")
    leaked = [p.name for p in out.iterdir() if p.name in NEVER]
    if leaked:
        print(f"помилка: у збірку потрапили {leaked}", file=sys.stderr)
        return 1
    archive = shutil.make_archive(str(DIST / f"UGV-Quick-Setup-{__version__}-{time.strftime('%Y%m%d')}"), "zip", DIST, APP_NAME)
    print(f"готово: {out}\n        {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
