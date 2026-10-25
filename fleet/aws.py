"""Новий сервер в AWS однією командою для CloudShell (кнопка вгорі майстра).

Скрипт — server/aws/create-server.sh: ключ SSH, група безпеки, сервер Ubuntu і постійна адреса.
Команду, як і команду для сервера hub, стискаємо в один рядок: багаторядкову вставку CloudShell
перепитує, а рядок вставляється й запускається одразу.
"""
from __future__ import annotations

import ipaddress
import re
import urllib.request
from pathlib import Path

from .hub import packed_command
from .i18n import T

SCRIPT = Path("server") / "aws" / "create-server.sh"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,40}$")  # з неї ж імена ключа, групи безпеки й ролі
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
DEFAULT_REGION = "eu-north-1"  # Стокгольм: близько до України, і кнопка Connect там перевірена
CHECKIP = "https://checkip.amazonaws.com"


class AwsError(ValueError):
    pass


def server_command(root: Path, name: str, region: str = DEFAULT_REGION, my_ip: str = "") -> str:
    """Команда для AWS CloudShell. my_ip — звідки дозволити SSH, крім кнопки Connect (порожньо — нізвідки)."""
    if not NAME_RE.match(name or ""):
        raise AwsError(T("назва сервера — латинські літери, цифри й дефіс, до 40 знаків (наприклад, NRK-Server-1)",
                         "the server name is latin letters, digits and dashes, up to 40 characters (e.g. NRK-Server-1)"))
    if not REGION_RE.match(region or ""):
        raise AwsError(T(f"некоректний регіон AWS: {region}", f"invalid AWS region: {region}"))
    if my_ip:
        try:
            ip = ipaddress.ip_address(my_ip)
        except ValueError:
            ip = None
        if ip is None or ip.version != 4 or not ip.is_global:
            raise AwsError(T(f"{my_ip} — не публічна адреса IPv4", f"{my_ip} is not a public IPv4 address"))
    return packed_command(root / SCRIPT, [name, region, my_ip])


def public_ip(timeout: float = 5) -> str | None:
    """Публічна адреса цього ноутбука, як її бачить AWS (для правила SSH). None — не вдалося дізнатися."""
    try:
        with urllib.request.urlopen(CHECKIP, timeout=timeout) as r:
            text = r.read(64).decode().strip()
        ip = ipaddress.ip_address(text)
    except (OSError, ValueError):
        return None
    return str(ip) if ip.version == 4 and ip.is_global else None
