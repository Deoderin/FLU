"""Деплой стека площадки и сервисного туннеля на Linux-сервер по SSH.

Используется системный ssh/scp (OpenSSH есть и в Windows 10/11), поэтому
ключи, агент и ~/.ssh/config работают как обычно.
"""
from __future__ import annotations

import subprocess
import tarfile
import tempfile
from pathlib import Path

from .inventory import Inventory, Site
from .keystore import SecretStore
from .render import Renderer

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
REMOTE_ROOT = "/opt/fleet"


class DeployError(Exception):
    pass


def ensure_site_key(inv: Inventory, store: SecretStore, site: Site, allow_new: bool = False, robot_id: int | None = None) -> bool:
    """Создать ключ сервера площадки, если его нет. True — ключ создан.

    Если на площадке уже есть роботы, а ключа нет, значит secrets/ потерян:
    новый ключ отключит этих роботов, поэтому без allow_new отказываемся.
    """
    if store.has_site(site.name):
        return False
    others = [r.name for r in inv.robots_on(site.name) if r.id != robot_id]
    if others and not allow_new:
        raise DeployError(
            f"нет ключа сервера площадки {site.name}, а на ней уже есть роботы: {', '.join(others)}. "
            f"Восстановите secrets/ из резервной копии. Новый ключ (--new-key) отключит этих роботов до перепрошивки"
        )
    store.site(site.name, create=True)
    return True


def ensure_service_key(inv: Inventory, store: SecretStore, allow_new: bool = False, robot_id: int | None = None) -> bool:
    if store.has_service():
        return False
    others = [r.name for r in inv.robots.values() if r.service_tunnel and r.provisioned and r.id != robot_id]
    if others and not allow_new:
        raise DeployError(
            f"нет ключа сервера сервисного туннеля, а роботы с ним уже есть: {', '.join(others)}. "
            f"Восстановите secrets/ из резервной копии или используйте --new-key"
        )
    store.service(create=True)
    return True


def ssh(user: str, host: str, command: str, capture: bool = False) -> str:
    r = subprocess.run(["ssh", *SSH_OPTS, f"{user}@{host}", command], text=True, capture_output=capture)
    if r.returncode != 0:
        detail = f"\n{r.stderr.strip()}" if capture and r.stderr else ""
        raise DeployError(f"ssh {user}@{host}: команда завершилась с кодом {r.returncode}{detail}")
    return r.stdout if capture else ""


def push_bundle(bundle: Path, host: str, user: str, remote_dir: str) -> None:
    """Залить папку на сервер одним архивом и запустить в ней install.sh."""
    remote_tmp = f"/tmp/fleet-{Path(remote_dir).name}.tgz"
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(Path(tmp) / "bundle.tgz", "w:gz") as tar:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(bundle).as_posix())
        # Относительный путь и cwd: scp на Windows путает «C:» с именем хоста
        r = subprocess.run(["scp", "-q", *SSH_OPTS, "bundle.tgz", f"{user}@{host}:{remote_tmp}"], cwd=tmp)
        if r.returncode != 0:
            raise DeployError(f"scp на {user}@{host} завершился с кодом {r.returncode}")
    sudo = "" if user == "root" else "sudo "
    ssh(user, host, (
        f"{sudo}mkdir -p {remote_dir} && {sudo}tar -xzf {remote_tmp} -C {remote_dir} "
        f"&& rm -f {remote_tmp} && {sudo}bash {remote_dir}/install.sh"
    ))


def deploy_site(inv: Inventory, store: SecretStore, site: Site, allow_new_key: bool = False, log=print) -> None:
    if not site.managed:
        raise DeployError(f"площадка {site.name} — external: сервер заказчика мы не ставим, для неё fleet kit")
    if not site.host:
        raise DeployError(f"у площадки {site.name} не задан host в inventory/sites.yaml")
    if ensure_site_key(inv, store, site, allow_new_key):
        log(f"создан ключ сервера площадки {site.name}")
    Renderer(inv, store).render_site(site)
    log(f"деплой {site.name} -> {site.ssh_user}@{site.host}:{REMOTE_ROOT}/{site.name}")
    push_bundle(inv.root / "build" / "sites" / site.name / "server", site.host, site.ssh_user, f"{REMOTE_ROOT}/{site.name}")


def deploy_service(inv: Inventory, store: SecretStore, allow_new_key: bool = False, log=print) -> None:
    svc = inv.service
    if svc is None or not svc.host:
        raise DeployError("раздел service (с host) в inventory/sites.yaml не задан")
    if ensure_service_key(inv, store, allow_new_key):
        log("создан ключ сервера сервисного туннеля")
    Renderer(inv, store).render_service()
    log(f"деплой сервисного туннеля -> {svc.ssh_user}@{svc.host}:{REMOTE_ROOT}/service")
    push_bundle(inv.root / "build" / "service" / "server", svc.host, svc.ssh_user, f"{REMOTE_ROOT}/service")
