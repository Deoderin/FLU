"""Termite QGroundControl (форк QGC від Tencore): камери НРК одразу в його налаштуваннях.

Termite зберігає налаштування у QSettings INI: %APPDATA%\\tencore.org\\Termit QGroundControl Daily.ini.
Розділ [TermitVideo] — джерело відео, [TermitCamera1..3] — камери. Записуємо лише свої ключі,
решту файлу (карта, телеметрія тощо) не чіпаємо.

Дві пастки, на які ми вже наступили:
- Termite, поки відкритий, тримає налаштування в пам'яті й під час закриття перезаписує файл — писати можна
  лише в закритий;
- Termite зібраний на QGroundControl 4.x, який передає GStreamer шлях до своїх модулів у неправильному
  кодуванні. Якщо в шляху до папки програми є кирилиця, відео не працює зовсім (у журналі
  «gst_element_factory_make('tee') failed»).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .i18n import T

SETTINGS_DIR = "tencore.org"
DEFAULT_FILE = "Termit QGroundControl Daily.ini"
CAMERA_NAMES = {"cam1": "front", "cam2": "back"}  # як у налаштуваннях пілотів
CAMERA_SECTIONS = {"cam1": "TermitCamera1", "cam2": "TermitCamera2"}

# Де стоїть Termite: запущений процес, ярлики на робочому столі й у меню «Пуск», записи інсталятора.
# Вивід — UTF-8: саме кириличний шлях нам і треба побачити неспотвореним.
PS_FIND = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$paths = @()
$running = @(Get-Process QGroundControl | Where-Object { $_.Path -match 'Termit' })
$paths += $running | ForEach-Object { $_.Path }
$sh = New-Object -ComObject WScript.Shell
$dirs = @('Desktop', 'CommonDesktopDirectory', 'StartMenu', 'CommonStartMenu') | ForEach-Object { [Environment]::GetFolderPath($_) }
$paths += Get-ChildItem $dirs -Recurse -Filter *.lnk | Where-Object { $_.Name -match 'Termit' } |
  ForEach-Object { $sh.CreateShortcut($_.FullName).TargetPath }
$keys = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
$paths += Get-ChildItem $keys | ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DisplayName -match 'Termit' } |
  ForEach-Object { if ($_.InstallLocation) { $_.InstallLocation } else { $_.DisplayIcon } }
@{ paths = @($paths | Where-Object { $_ } | Select-Object -Unique); running = ($running.Count -gt 0) } | ConvertTo-Json -Compress
"""


class TermiteError(Exception):
    pass


def settings_path() -> Path | None:
    """Файл налаштувань Termite. Якщо Termite ще не запускали, файлу немає — створимо з його звичною назвою."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / SETTINGS_DIR
    found = sorted(base.glob("Termit QGroundControl*.ini"))
    return found[0] if found else base / DEFAULT_FILE


def find_installs() -> dict:
    """{"paths": [...], "running": bool}: де стоїть Termite і чи він зараз відкритий."""
    if os.name != "nt":
        return {"paths": [], "running": False}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", PS_FIND], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {"paths": [], "running": False}
    paths = data.get("paths") or []
    return {"paths": [paths] if isinstance(paths, str) else list(paths), "running": bool(data.get("running"))}


def non_ascii_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if not p.isascii()]


def ini_value(value: object) -> str:
    """Значення у форматі QSettings: рядок із , ; = чи пробілами по краях — у лапках, як пише сам Qt."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if any(c in text for c in ',;="\\') or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def update_ini(text: str, values: dict[str, dict[str, object]]) -> str:
    """Замінити або дописати ключі в потрібних розділах; інші рядки файлу лишаються як є."""
    newline = "\r\n" if "\r\n" in text or not text else "\n"
    lines = text.splitlines()
    out: list[str] = []
    pending = {section: dict(keys) for section, keys in values.items()}
    section = None

    def flush(sec: str | None) -> None:
        for key, value in pending.pop(sec, {}).items():
            out.append(f"{key}={ini_value(value)}")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if out and out[-1] == "" and section in pending:  # дописуємо перед порожнім рядком між розділами
                out.pop()
                flush(section)
                out.append("")
            else:
                flush(section)
            section = stripped[1:-1]
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if section in pending and "=" in line and key in pending[section]:
            out.append(f"{key}={ini_value(pending[section].pop(key))}")
            continue
        out.append(line)
    flush(section)
    for sec, keys in pending.items():  # розділів не було зовсім
        if out and out[-1] != "":
            out.append("")
        out.append(f"[{sec}]")
        out.extend(f"{key}={ini_value(value)}" for key, value in keys.items())
    return newline.join(out) + newline


def camera_values(streams: list[dict]) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {
        "TermitVideo": {"videoSource": "RTSP Video Stream", "streamEnabled": True, "lowLatencyMode": True},
    }
    for s in streams:
        if s["cam"] in CAMERA_SECTIONS:
            values[CAMERA_SECTIONS[s["cam"]]] = {
                "isEnabled": True, "visibleName": CAMERA_NAMES[s["cam"]],
                "rtspUrl": s["rtsp"], "secondaryRtspUrl": s.get("rtsp2") or s["rtsp"],
            }
    return values


def write_cameras(streams: list[dict], path: Path | None = None, installs: dict | None = None) -> dict:
    """Прописати камери НРК у Termite. Повертає {"path": ..., "warnings": [...]}."""
    if not streams:
        raise TermiteError(T("у НРК ще немає адрес камер: спершу зареєструйте його на сервері",
                             "the UGV has no camera addresses yet: register it on the server first"))
    installs = find_installs() if installs is None else installs
    if installs["running"]:
        raise TermiteError(T(
            "закрийте Termite QGroundControl і натисніть ще раз: відкритий Termite під час закриття перезапише свої налаштування",
            "close Termite QGroundControl and press again: an open Termite overwrites its settings when it closes"))
    path = path or settings_path()
    if path is None:
        raise TermiteError(T("не знайшов, куди Termite зберігає налаштування (немає %APPDATA%)",
                             "could not find where Termite keeps its settings (no %APPDATA%)"))
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_bytes().decode("utf-8") if path.exists() else ""
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(update_ini(old, camera_values(streams)))
    warnings = []
    for p in non_ascii_paths(installs["paths"]):
        warnings.append(T(
            f"Termite лежить у папці з кирилицею ({p}): у ньому не працюватиме відео. Скопіюйте всю папку програми "
            f"в C:\\Termit і запускайте звідти — налаштування залишаться",
            f"Termite is in a folder with Cyrillic letters ({p}): its video will not work. Copy the whole program "
            f"folder to C:\\Termit and run it from there - the settings stay"))
    return {"path": str(path), "warnings": warnings}
