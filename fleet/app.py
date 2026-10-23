"""UGV Quick Setup у власному вікні, без браузера.

Той самий майстер, що й у fleet ui, але вікно — WebView2 (рушій Edge, вбудований у Windows 10/11)
через pywebview, а сервер майстра працює в цьому ж процесі. Закрили вікно — програма завершилась.
Без WebView2 чи pywebview майстер відкривається в браузері, як раніше.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_NAME, NO_WINDOW, ROOT
from .i18n import T
from .ui import UiServer, ui_running

WINDOW = {"width": 1180, "height": 900, "min_size": (720, 600)}


def data_dir() -> Path:
    """Дані вікна (localStorage майстра: вибраний НРК, мова) — у профілі користувача, не в теці програми."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "webview"


def show_error(text: str) -> None:
    """У вікні без консолі print нікому не видно: помилку показуємо діалогом Windows."""
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
    else:
        print(text, file=sys.stderr)


def ensure_desktop_shortcut(exe: Path) -> None:
    """Зібрана програма сама кладе ярлик на робочий стіл, якщо його ще немає (наявний не чіпаємо)."""
    if os.name != "nt":
        return
    script = (
        "$d = [Environment]::GetFolderPath('Desktop'); $lnk = Join-Path $d '" + APP_NAME + ".lnk'; "
        "if (-not (Test-Path $lnk)) { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); "
        f"$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{exe.parent}'; $s.IconLocation = '{exe},0'; $s.Save() }}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=30,
                       capture_output=True, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass  # без ярлика програма однаково працює


def run_app(root: Path = ROOT, port: int = 8765) -> int:
    url = f"http://127.0.0.1:{port}/"
    try:
        server = UiServer(("127.0.0.1", port), root)
    except OSError:
        if not ui_running(url):
            show_error(T(f"Порт {port} зайнятий іншою програмою. Закрийте її й запустіть {APP_NAME} ще раз.",
                         f"Port {port} is used by another program. Close it and start {APP_NAME} again."))
            return 2
        server = None  # уже відкрито: ще одне вікно до того самого майстра
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if getattr(sys, "frozen", False):
        ensure_desktop_shortcut(Path(sys.executable).resolve())
    try:
        try:
            import webview
        except ImportError:
            return _browser_fallback(url, server)
        webview.create_window(APP_NAME, url, **WINDOW)
        try:
            data_dir().mkdir(parents=True, exist_ok=True)
            # private_mode=False: майстер пам'ятає вибраний НРК і мову між запусками
            webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False, storage_path=str(data_dir()))
        except Exception:  # немає WebView2 (дуже старі Windows) — відкриваємо в браузері
            return _browser_fallback(url, server)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return 0


def _browser_fallback(url: str, server: UiServer | None) -> int:
    webbrowser.open(url)
    if server is None:
        return 0
    threading.Event().wait()  # сервер працює, поки процес не закриють
    return 0


def main() -> None:
    """Точка входу зібраної програми «UGV Quick Setup.exe» (--port N — друга копія поруч із першою)."""
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args[:-1] else 8765
    try:
        sys.exit(run_app(port=port))
    except Exception as exc:  # без консолі трасування ніхто не побачить
        show_error(T(f"{APP_NAME} не запустився: {exc}", f"{APP_NAME} failed to start: {exc}"))
        sys.exit(1)


if __name__ == "__main__":  # ярлик: pythonw -m fleet.app
    main()
