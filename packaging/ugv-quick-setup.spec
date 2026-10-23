# PyInstaller: dist/UGV Quick Setup/ з двома програмами на спільних бібліотеках.
#   UGV Quick Setup.exe — вікно майстра (без консолі);
#   fleet.exe           — консольні команди, ними вікно запускає прошивку й тести.
# Збирає packaging/build.py, він же кладе поруч дані (base/, templates/, ...). Значок — у змінній UGV_ICON.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = os.environ.get("UGV_ICON") or None

# fleet імпортує частину модулів усередині функцій, pymavlink — діалекти за назвою
HIDDEN = collect_submodules("fleet") + [
    "pymavlink.dialects.v20.ardupilotmega",
    "pymavlink.dialects.v20.common",
    "pymavlink.dialects.v10.ardupilotmega",
]
DATAS = [(str(ROOT / "fleet" / "simple.html"), "fleet"), (str(ROOT / "fleet" / "ui.html"), "fleet")]
EXCLUDES = ["tkinter", "pytest", "PyYAML", "yaml", "aiohttp", "playwright"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=DATAS,
                    hiddenimports=HIDDEN, excludes=EXCLUDES)


gui = analysis("gui_main.py")
cli = analysis("cli_main.py")

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="UGV Quick Setup",
              console=False, icon=ICON, upx=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="fleet",
              console=True, icon=ICON, upx=False)

COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas,
        name="UGV Quick Setup", upx=False)
