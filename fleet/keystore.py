"""Секрети: ключі WireGuard і паролі.

Лежать у secrets/ (у .gitignore), по файлу на об'єкт:
  secrets/robots/<id>.json      ключі тунелів НРК, паролі MikroTik і камер
  secrets/sites/<name>.json     ключ сервера managed-майданчика
  secrets/service.json          ключ сервера сервісного тунелю
  secrets/operators/<name>.json, secrets/engineers/<name>.json

Ключі НРК і серверів створюються лише явно (provision, deploy-server):
якщо тихо перестворити такий ключ, НРК втратять зв'язок. Ключі операторів та
інженерів створюються під час render — новий ключ означає лише новий конфіг оператору.
"""
from __future__ import annotations

import base64
import json
import secrets as rnd
import string
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .i18n import T


class SecretError(Exception):
    pass


def wg_genkey() -> str:
    """Закритий ключ WireGuard, як `wg genkey`."""
    raw = bytearray(rnd.token_bytes(32))
    raw[0] &= 248
    raw[31] = (raw[31] & 127) | 64
    return base64.b64encode(bytes(raw)).decode()


def wg_pubkey(private: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private))
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def gen_password(length: int = 16) -> str:
    """Лише літери та цифри: без екранування в RouterOS, URL RTSP і API камер."""
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(rnd.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            return p


ROBOT_FIELDS: dict[str, Callable[[], str]] = {
    "wg_site": wg_genkey,
    "wg_svc": wg_genkey,
    "mikrotik_password": gen_password,
    "camera_password": lambda: gen_password(12),
}
KEY_FIELDS: dict[str, Callable[[], str]] = {"wg": wg_genkey}


class SecretStore:
    def __init__(self, root: Path):
        self.dir = root / "secrets"

    def _get(self, rel: str, fields: dict[str, Callable[[], str]], create: bool, hint: str) -> dict:
        path = self.dir / rel
        data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        missing = [k for k in fields if k not in data]
        if missing:
            if not create:
                raise SecretError(T(f"немає секретів secrets/{rel} ({', '.join(missing)}): {hint}",
                                    f"secrets/{rel} is missing ({', '.join(missing)}): {hint}"))
            for k in missing:
                data[k] = fields[k]()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return data

    def has_site(self, name: str) -> bool:
        return (self.dir / "sites" / f"{name}.json").exists()

    def has_service(self) -> bool:
        return (self.dir / "service.json").exists()

    def robot(self, robot_id: int, create: bool = False) -> dict:
        return self._get(f"robots/{robot_id}.json", ROBOT_FIELDS, create,
                         T(f"спочатку fleet provision --id {robot_id}", f"run fleet provision --id {robot_id} first"))

    def site(self, name: str, create: bool = False) -> dict:
        return self._get(f"sites/{name}.json", KEY_FIELDS, create,
                         T(f"спочатку fleet deploy-server --site {name}", f"run fleet deploy-server --site {name} first"))

    def service(self, create: bool = False) -> dict:
        return self._get("service.json", KEY_FIELDS, create,
                         T("спочатку fleet deploy-server --service", "run fleet deploy-server --service first"))

    def operator(self, name: str) -> dict:
        return self._get(f"operators/{name}.json", KEY_FIELDS, True, "")

    def engineer(self, name: str) -> dict:
        return self._get(f"engineers/{name}.json", KEY_FIELDS, True, "")
