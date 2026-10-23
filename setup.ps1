<#
  Установник UGV Quick Setup для ноутбука (Windows 10/11).

  1. Python 3.14 (або вже встановлений 3.11-3.14)
  2. WireGuard
  3. Оточення .venv і сам fleet
  4. Ярлик «UGV Quick Setup» на робочому столі
  5. Відкриває майстер першого налаштування в браузері

  Ставить через winget, а якщо його немає — завантажує інсталятори з python.org
  і wireguard.com. Запуск: двічі клацнути setup.cmd.
  Повторний запуск безпечний: те, що вже є, пропускається.

  -DryRun      лише показати, що буде зроблено
  -NoWireGuard не ставити WireGuard
  -NoShortcut  не створювати ярлик
  -NoLaunch    не відкривати майстер наприкінці
#>
param([switch]$DryRun, [switch]$NoWireGuard, [switch]$NoShortcut, [switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12  # старі Windows за замовчуванням TLS 1.0

$Root = $PSScriptRoot
$PyWinget = 'Python.Python.3.14'
$PyUrl = 'https://www.python.org/ftp/python/3.14.8/python-3.14.8-amd64.exe'
$WgUrl = 'https://download.wireguard.com/windows-client/wireguard-installer.exe'
$WgExe = Join-Path $env:ProgramFiles 'WireGuard\wireguard.exe'
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'

function Step($n, $text) { Write-Host ''; Write-Host "[$n/5] $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Info($text) { Write-Host "      $text" }
function Fail($text) { Write-Host "  ПОМИЛКА  $text" -ForegroundColor Red; exit 1 }

function Run-Native([string]$exe, [string[]]$arguments) {
  # Windows PowerShell 5.1 перетворює stderr зовнішніх програм на помилки:
  # запускаємо їх без зупинки й дивимося лише на код виходу
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { & $exe @arguments 2>&1 | ForEach-Object { Info "$_" } } finally { $ErrorActionPreference = $old }
  return $LASTEXITCODE
}

function Python-Version([string]$exe) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  # Без лапок усередині: PowerShell 5.1 губить їх в аргументах зовнішніх програм
  try { $v = & $exe -c 'import sys; print(*sys.version_info[:2], sep=chr(46))' 2>$null } finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -eq 0) { return "$v".Trim() } else { return $null }
}

function Find-Python {
  # Підходять 3.11-3.14: під 3.15 ще немає готових збірок pymavlink і PyYAML
  $ok = '3.11', '3.12', '3.13', '3.14'
  $found = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $list = & py -0p 2>$null } finally { $ErrorActionPreference = $old }
    foreach ($line in $list) { if ("$line" -match '([A-Za-z]:\\.*python\.exe)') { $found += $Matches[1] } }
  }
  foreach ($v in '314', '313', '312', '311') {
    $found += "$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe", "$env:ProgramFiles\Python$v\python.exe"
  }
  $best = $null; $bestVer = $null
  foreach ($exe in ($found | Select-Object -Unique)) {
    if (-not (Test-Path $exe)) { continue }
    $v = Python-Version $exe
    if ($v -and ($ok -contains $v) -and (-not $bestVer -or [version]$v -gt [version]$bestVer)) { $best = $exe; $bestVer = $v }
  }
  return $best
}

function Install-Python {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Info "winget install $PyWinget"
    $code = Run-Native 'winget' @('install', '-e', '--id', $PyWinget, '--scope', 'user', '--silent',
                                  '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if ($code -eq 0) { return }
    Info 'winget не впорався, завантажую інсталятор з python.org'
  }
  $tmp = Join-Path $env:TEMP 'python-3.14-amd64.exe'
  Invoke-WebRequest -UseBasicParsing -Uri $PyUrl -OutFile $tmp
  Start-Process -FilePath $tmp -Wait -ArgumentList '/quiet', 'InstallAllUsers=0', 'Include_launcher=1',
    'InstallLauncherAllUsers=0', 'PrependPath=0', 'Include_test=0'
}

function Install-WireGuard {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Info 'winget install WireGuard.WireGuard (Windows спитає дозвіл адміністратора)'
    $code = Run-Native 'winget' @('install', '-e', '--id', 'WireGuard.WireGuard', '--silent',
                                  '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if ($code -eq 0 -or (Test-Path $WgExe)) { return }
    Info 'winget не впорався, завантажую інсталятор з wireguard.com'
  }
  $tmp = Join-Path $env:TEMP 'wireguard-installer.exe'
  Invoke-WebRequest -UseBasicParsing -Uri $WgUrl -OutFile $tmp
  Start-Process -FilePath $tmp -Wait  # сам попросить дозвіл адміністратора
}

Write-Host 'UGV Quick Setup: установка на ноутбук' -ForegroundColor White
if ($DryRun) { Write-Host '(dry-run: нічого не змінюю, лише показую план)' -ForegroundColor Yellow }

# 1. Python ---------------------------------------------------------------------
Step 1 'Python'
$Python = Find-Python
if ($Python) { Ok "$Python ($(Python-Version $Python))" }
elseif ($DryRun) { Info "поставив би Python 3.14 ($PyWinget)" }
else {
  Install-Python
  $Python = Find-Python
  if (-not $Python) { Fail 'Python не знайдено після встановлення. Поставте Python 3.14 з python.org і запустіть setup.cmd ще раз' }
  Ok "$Python ($(Python-Version $Python))"
}

# 2. WireGuard ------------------------------------------------------------------
Step 2 'WireGuard'
if ($NoWireGuard) { Info 'пропущено (-NoWireGuard)' }
elseif (Test-Path $WgExe) { Ok $WgExe }
elseif ($DryRun) { Info 'поставив би WireGuard (WireGuard.WireGuard)' }
else {
  Install-WireGuard
  if (Test-Path $WgExe) { Ok $WgExe } else { Fail 'WireGuard не встановився. Поставте його з wireguard.com і запустіть setup.cmd ще раз' }
}

# 3. Оточення й fleet -----------------------------------------------------------
Step 3 'Оточення .venv і fleet'
if ((Test-Path $VenvPy) -and -not (@('3.11', '3.12', '3.13', '3.14') -contains (Python-Version $VenvPy))) {
  Info 'старе .venv на непідходящому Python — створюю заново'
  if (-not $DryRun) { Remove-Item -Recurse -Force (Join-Path $Root '.venv') }
}
if ($DryRun) { Info "python -m venv .venv; pip install -e $Root" }
else {
  if (-not (Test-Path $VenvPy)) {
    if ((Run-Native $Python @('-m', 'venv', (Join-Path $Root '.venv'))) -ne 0) { Fail 'не вдалося створити .venv' }
  }
  Info 'pip install (потрібен інтернет, ~30 МБ)'
  if ((Run-Native $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-e', $Root)) -ne 0) {
    Fail 'pip не зміг поставити залежності. Перевірте інтернет (або проксі) і запустіть setup.cmd ще раз'
  }
  # Позначка для fleet-ui.cmd: залежності відповідають цьому pyproject.toml, оновлювати не треба
  Copy-Item -Force (Join-Path $Root 'pyproject.toml') (Join-Path $Root '.venv\pyproject.installed')
  if ((Run-Native (Join-Path $Root '.venv\Scripts\fleet.exe') @('check')) -ne 0) { Fail 'fleet check знайшов помилки в inventory/' }
  Ok 'fleet встановлено'
}

# 4. Ярлик ----------------------------------------------------------------------
Step 4 'Ярлик на робочому столі'
$Lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'UGV Quick Setup.lnk'
# Ярлик зі старою тестовою назвою прибираємо, щоб на столі не було двох
$OldLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FLU fleet.lnk'
if ($NoShortcut) { Info 'пропущено (-NoShortcut)' }
elseif ($DryRun) { Info "створив би $Lnk" }
else {
  $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($Lnk)
  $shortcut.TargetPath = Join-Path $Root 'fleet-ui.cmd'
  $shortcut.WorkingDirectory = $Root
  $shortcut.IconLocation = Join-Path $Root 'packaging\app.ico'
  $shortcut.WindowStyle = 7  # fleet-ui.cmd лише згорнуто мигне, далі відкривається вікно майстра
  $shortcut.Save()
  Remove-Item -Force -ErrorAction SilentlyContinue $OldLnk
  Ok $Lnk
}

# 5. Майстер --------------------------------------------------------------------
Step 5 'Майстер першого налаштування'
if ($NoLaunch -or $DryRun) { Info 'відкрийте ярлик «UGV Quick Setup» або fleet-ui.cmd' }
else {
  Start-Process -FilePath (Join-Path $Root 'fleet-ui.cmd') -WorkingDirectory $Root
  Ok 'відкриваю вікно майстра — далі: адреса сервера, ім''я пілота, номер НРК'
}
