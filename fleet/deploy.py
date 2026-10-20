"""Розгортання стеку майданчика та сервісного тунелю на Linux-сервер по SSH.

Використовується системний ssh/scp (OpenSSH є і в Windows 10/11), тож
ключі, агент і ~/.ssh/config працюють як зазвичай.
"""
from __future__ import annotations

import json
import subprocess
import time
import tarfile
import tempfile
from pathlib import Path

from .i18n import T
from .inventory import Inventory, Site
from .keystore import SecretStore
from .render import Renderer

# accept-new: ключ нового сервера запам'ятовується під час першого підключення,
# а підмінений ключ відомого сервера — відхиляється
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]
REMOTE_ROOT = "/opt/fleet"


class DeployError(Exception):
    pass


def ensure_site_key(inv: Inventory, store: SecretStore, site: Site, allow_new: bool = False, robot_id: int | None = None) -> bool:
    """Створити ключ сервера майданчика, якщо його немає. True — ключ створено.

    Якщо на майданчику вже є НРК, а ключа немає, значить secrets/ втрачено:
    новий ключ від'єднає ці НРК, тому без allow_new відмовляємо.
    """
    if store.has_site(site.name):
        return False
    others = [r.name for r in inv.robots_on(site.name) if r.id != robot_id]
    if others and not allow_new:
        raise DeployError(T(
            f"немає ключа сервера майданчика {site.name}, а на ньому вже є НРК: {', '.join(others)}. "
            f"Відновіть secrets/ з резервної копії. Новий ключ (--new-key) від'єднає ці НРК до перепрошивки",
            f"the server key of site {site.name} is missing, but the site already has UGVs: {', '.join(others)}. "
            f"Restore secrets/ from a backup. A new key (--new-key) disconnects these UGVs until they are reflashed",
        ))
    store.site(site.name, create=True)
    return True


def ensure_service_key(inv: Inventory, store: SecretStore, allow_new: bool = False, robot_id: int | None = None) -> bool:
    if store.has_service():
        return False
    others = [r.name for r in inv.robots.values() if r.service_tunnel and r.provisioned and r.id != robot_id]
    if others and not allow_new:
        raise DeployError(T(
            f"немає ключа сервера сервісного тунелю, а НРК із ним уже є: {', '.join(others)}. "
            f"Відновіть secrets/ з резервної копії або використайте --new-key",
            f"the service tunnel server key is missing, but UGVs with it already exist: {', '.join(others)}. "
            f"Restore secrets/ from a backup or use --new-key",
        ))
    store.service(create=True)
    return True


def ssh(user: str, host: str, command: str, capture: bool = False) -> str:
    r = subprocess.run(["ssh", *SSH_OPTS, f"{user}@{host}", command], text=True, capture_output=capture)
    if r.returncode != 0:
        detail = f"\n{r.stderr.strip()}" if capture and r.stderr else ""
        raise DeployError(T(f"ssh {user}@{host}: команда завершилася з кодом {r.returncode}{detail}",
                            f"ssh {user}@{host}: the command exited with code {r.returncode}{detail}"))
    return r.stdout if capture else ""


def push_bundle(bundle: Path, host: str, user: str, remote_dir: str) -> None:
    """Залити теку на сервер одним архівом і запустити в ній install.sh."""
    remote_tmp = f"/tmp/fleet-{Path(remote_dir).name}.tgz"
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(Path(tmp) / "bundle.tgz", "w:gz") as tar:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(bundle).as_posix())
        # Відносний шлях і cwd: scp у Windows плутає «C:» з іменем хоста
        r = subprocess.run(["scp", "-q", *SSH_OPTS, "bundle.tgz", f"{user}@{host}:{remote_tmp}"], cwd=tmp)
        if r.returncode != 0:
            raise DeployError(T(f"scp на {user}@{host} завершився з кодом {r.returncode}",
                                f"scp to {user}@{host} exited with code {r.returncode}"))
    sudo = "" if user == "root" else "sudo "
    ssh(user, host, (
        f"{sudo}mkdir -p {remote_dir} && {sudo}tar -xzf {remote_tmp} -C {remote_dir} "
        f"&& rm -f {remote_tmp} && {sudo}bash {remote_dir}/install.sh"
    ))


def deploy_site(inv: Inventory, store: SecretStore, site: Site, allow_new_key: bool = False, log=print) -> None:
    if not site.managed:
        raise DeployError(T(f"майданчик {site.name} — external: сервер замовника ми не ставимо, для нього fleet kit",
                            f"site {site.name} is external: we do not install the customer's server, use fleet kit"))
    if not site.host:
        raise DeployError(T(f"у майданчика {site.name} не задано host в inventory/sites.yaml",
                            f"site {site.name} has no host in inventory/sites.yaml"))
    if ensure_site_key(inv, store, site, allow_new_key):
        log(T(f"створено ключ сервера майданчика {site.name}", f"created the server key of site {site.name}"))
    Renderer(inv, store).render_site(site)
    log(T(f"розгортання {site.name} -> {site.ssh_user}@{site.host}:{REMOTE_ROOT}/{site.name}",
          f"deploying {site.name} -> {site.ssh_user}@{site.host}:{REMOTE_ROOT}/{site.name}"))
    push_bundle(inv.root / "build" / "sites" / site.name / "server", site.host, site.ssh_user, f"{REMOTE_ROOT}/{site.name}")
    # Позначка для інтерфейсу: сервер розгорнуто, і коли саме
    marker = inv.root / "build" / "sites" / site.name / "deployed.json"
    marker.write_text(json.dumps({"host": site.host, "time": time.strftime("%Y-%m-%d %H:%M")}), "utf-8")


def deploy_service(inv: Inventory, store: SecretStore, allow_new_key: bool = False, log=print) -> None:
    svc = inv.service
    if svc is None or not svc.host:
        raise DeployError(T("розділ service (з host) в inventory/sites.yaml не задано",
                            "the service section (with host) is missing in inventory/sites.yaml"))
    if ensure_service_key(inv, store, allow_new_key):
        log(T("створено ключ сервера сервісного тунелю", "created the service tunnel server key"))
    Renderer(inv, store).render_service()
    log(T(f"розгортання сервісного тунелю -> {svc.ssh_user}@{svc.host}:{REMOTE_ROOT}/service",
          f"deploying the service tunnel -> {svc.ssh_user}@{svc.host}:{REMOTE_ROOT}/service"))
    push_bundle(inv.root / "build" / "service" / "server", svc.host, svc.ssh_user, f"{REMOTE_ROOT}/service")
