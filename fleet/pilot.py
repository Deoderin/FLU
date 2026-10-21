"""Тунель пілота у WireGuard на цьому ноутбуці (Windows).

Конфіг кладеться в теку WireGuard Data\\Configurations: менеджер WireGuard сам шифрує
його в .conf.dpapi, і тунель видно в застосунку. Потім тунель вмикається як служба
командою /installtunnelservice. Для цього потрібен дозвіл адміністратора (UAC).

У кожного пілота свій тунель (flu-<майданчик>-<пілот>): на одному ноутбуці технік може
налаштувати кількох пілотів. Тунелі ведуть у ті самі мережі НРК, тож працює лише один:
вмикаючи тунель пілота, вимикаємо тунелі інших (служба знімається, конфіг лишається в застосунку).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .i18n import T

WG_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WireGuard" / "wireguard.exe"
WG_CONFIGS = WG_EXE.parent / "Data" / "Configurations"


class TunnelError(Exception):
    pass


def ps_quote(value) -> str:
    """Значення для рядка PowerShell в одинарних лапках: апостроф подвоюється (O'Brien -> O''Brien)."""
    return str(value).replace("'", "''")


def tunnel_name(site: str, pilot: str) -> str:
    return f"flu-{site}-{pilot}"[:32]  # ім'я пілота — до 20 символів, тож обрізати не доводиться


def legacy_tunnel_name(site: str) -> str:
    """Один тунель на майданчик, як було раніше: при встановленні тунелю пілота його прибираємо."""
    return f"flu-{site}"[:32]


def wireguard_installed() -> bool | None:
    return WG_EXE.exists() if os.name == "nt" else None


def tunnel_status(name: str) -> str:
    """running | stopped | missing | unsupported (не Windows)."""
    if os.name != "nt":
        return "unsupported"
    r = subprocess.run(["sc.exe", "query", f"WireGuardTunnel${name}"], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        return "missing"
    return "running" if "RUNNING" in r.stdout else "stopped"


ELEVATED_PS1 = r"""$ErrorActionPreference = 'Stop'
$log = '{result}'
function Say($m) {{ Add-Content -LiteralPath $log -Value $m -Encoding UTF8 }}
try {{
  $wg = '{wg}'; $dir = '{configs}'; $name = '{name}'; $local = '{local}'; $legacy = '{legacy}'; $prefix = '{prefix}'
  # Знімаємо службу тунелю, лише якщо вона є: інакше wireguard.exe показує вікно з помилкою
  function Stop-Tunnel($n) {{
    if (Get-Service -Name ('WireGuardTunnel$' + $n) -ErrorAction SilentlyContinue) {{
      Start-Process -FilePath $wg -ArgumentList '/uninstalltunnelservice', $n -Wait -WindowStyle Hidden
      Say "stopped: $n"
    }}
  }}
  # Тунелі інших пілотів ведуть у ті самі мережі НРК: вимикаємо, їхні конфіги лишаються в застосунку
  Get-Service -Name ('WireGuardTunnel$' + $prefix + '*') -ErrorAction SilentlyContinue |
    ForEach-Object {{ $_.Name.Substring('WireGuardTunnel$'.Length) }} | Where-Object {{ $_ -ne $name }} |
    ForEach-Object {{ Stop-Tunnel $_ }}
  # Старий спільний тунель майданчика і попередній конфіг цього пілота прибираємо зовсім
  foreach ($n in @($legacy, $name)) {{
    Stop-Tunnel $n
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $dir "$n.conf.dpapi"), (Join-Path $dir "$n.conf")
  }}
  New-Item -ItemType Directory -Force $dir | Out-Null
  Copy-Item -Force -LiteralPath $local (Join-Path $dir "$name.conf")
  # Менеджер WireGuard сам шифрує нові .conf у цій теці в .conf.dpapi — так тунель видно в застосунку
  Start-Process -FilePath $wg
  $dpapi = Join-Path $dir "$name.conf.dpapi"
  for ($i = 0; $i -lt 30 -and -not (Test-Path $dpapi); $i++) {{ Start-Sleep -Seconds 1 }}
  if (Test-Path $dpapi) {{ $conf = $dpapi; Say 'conf: dpapi' }}
  else {{ Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $dir "$name.conf"); $conf = $local; Say 'conf: local' }}
  $p = Start-Process -FilePath $wg -ArgumentList '/installtunnelservice', ('"' + $conf + '"') -Wait -PassThru
  Say ("exit " + $p.ExitCode)
}} catch {{ Say ("ERROR " + $_.Exception.Message) }}
"""


def install_pilot_tunnel(root: Path, site: str, pilot: str, dry_run: bool = False, log=print) -> str:
    conf = root / "build" / "sites" / site / "operators" / f"{pilot}.conf"
    if not conf.exists():
        raise TunnelError(T(f"конфігу пілота {pilot} ще немає: спочатку вставте відповідь сервера",
                            f"there is no config for pilot {pilot} yet: paste the server response first"))
    name = tunnel_name(site, pilot)
    store = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FLU" / "wireguard"
    store.mkdir(parents=True, exist_ok=True)
    local = store / f"{name}.conf"
    shutil.copyfile(conf, local)

    if os.name != "nt":
        log(T(f"Не Windows: імпортуйте {local} у WireGuard вручну (wg-quick up {local})",
              f"Not Windows: import {local} into WireGuard manually (wg-quick up {local})"))
        return "unsupported"
    if not WG_EXE.exists() and not dry_run:
        raise TunnelError(T("WireGuard не встановлено: запустіть setup.cmd ще раз",
                            "WireGuard is not installed: run setup.cmd again"))

    result = Path(tempfile.gettempdir()) / f"fleet-wg-{name}.log"
    script = ELEVATED_PS1.format(**{k: ps_quote(v) for k, v in {
        "result": result, "wg": WG_EXE, "configs": WG_CONFIGS, "name": name, "local": local,
        "legacy": legacy_tunnel_name(site), "prefix": f"flu-{site}-"}.items()})
    ps1 = Path(tempfile.gettempdir()) / f"fleet-wg-{name}.ps1"
    ps1.write_text(script, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 інакше читає файл як ANSI
    if dry_run:
        log(script)
        return "dry-run"

    result.unlink(missing_ok=True)
    log(T(f"Windows попросить дозвіл адміністратора для WireGuard — натисніть «Так» (тунель {name})",
          f"Windows will ask for administrator permission for WireGuard - press Yes (tunnel {name})"))
    r = subprocess.run([
        "powershell", "-NoProfile", "-Command",
        f"Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"{ps_quote(ps1)}\"'",
    ], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise TunnelError(T("дозвіл адміністратора не надано — тунель не встановлено",
                            "administrator permission was not granted - the tunnel is not installed"))
    for line in result.read_text("utf-8-sig").splitlines() if result.exists() else []:
        log(f"  WireGuard: {line}")
    status = tunnel_status(name)
    if status != "running":
        raise TunnelError(T(f"тунель {name} не запустився (стан: {status}); подивіться журнал у застосунку WireGuard",
                            f"tunnel {name} did not start (state: {status}); check the log in the WireGuard app"))
    log(T(f"тунель {name} працює; тунелі інших пілотів на цьому ноутбуці вимкнено",
          f"tunnel {name} is up; the tunnels of other pilots on this laptop are off"))
    return status
