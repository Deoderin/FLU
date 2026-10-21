import ipaddress
import shutil

import pytest

from fleet import addressing as A
from fleet.i18n import set_lang
from fleet.inventory import Inventory, InventoryError, Robot

from .conftest import REPO


def test_repo_inventory_is_valid():
    """Справжній інвентар репозиторію (без тестових бортів) теж має проходити перевірку."""
    inv = Inventory.load(REPO)
    assert inv.validate() == []
    assert "main" in inv.sites


def test_fixture_inventory_is_valid(inv):
    assert inv.validate() == []
    assert set(inv.sites) == {"flu-acme", "globex", "initech"}
    assert inv.robot(1).name == "nrk-001"


def test_addresses_follow_slot(inv):
    acme = inv.site("flu-acme")
    assert str(A.server_ip(acme)) == "10.150.0.1"
    assert str(A.robot_tunnel_ip(acme, 1)) == "10.150.0.2"
    assert str(A.lan_block(acme, 1)) == "10.150.16.0/28"
    assert str(A.lan_block(acme, 2)) == "10.150.16.16/28"
    assert str(A.mapped_ip(acme, 2, "cam2")) == "10.150.16.25"  # задня камера ззовні — .9 блоку (у НРК — .109)
    assert A.arbiter_port(3) == 5763
    assert A.capacity(acme) == 250
    assert A.capacity(inv.site("initech")) == 4  # robot_lans /26 = чотири блоки /28


def test_service_addresses(inv):
    svc = inv.service
    assert str(A.service_server_ip(svc)) == "10.99.0.1"
    assert str(A.service_engineer_ip(svc, svc.engineers[0])) == "10.99.0.2"
    assert str(A.service_robot_ip(svc, 1)) == "10.99.1.1"
    assert A.service_robot_ip(svc, 1) not in A.service_engineers_net(svc)


def test_overlapping_sites_rejected(inv):
    inv.sites["globex"].network.robot_lans = ipaddress.ip_network("10.150.16.0/24")
    assert any("перетинається" in e for e in inv.validate())


def test_robot_lan_and_cgnat_rejected(inv):
    inv.sites["globex"].network.tunnel = ipaddress.ip_network("192.168.88.0/26")
    inv.sites["globex"].network.operators = ipaddress.ip_network("100.64.1.0/24")
    errors = " ".join(inv.validate())
    assert "внутрішньою мережею НРК" in errors
    assert "CGNAT" in errors


def test_messages_in_english(inv):
    inv.sites["globex"].network.robot_lans = ipaddress.ip_network("10.150.16.0/24")
    set_lang("en")
    assert any("overlaps" in e for e in inv.validate())
    with pytest.raises(InventoryError, match="is not in inventory/robots.yaml"):
        inv.robot(99)


def test_external_site_needs_server_key(inv):
    inv.sites["initech"].server_pubkey = "not-a-key"
    assert any("server_pubkey" in e for e in inv.validate())


def test_operators_only_on_managed_sites(inv):
    inv.operators["ivanenko"].sites.append("initech")
    assert any("лише на hub і managed" in e for e in inv.validate())


def test_duplicate_slot_rejected(inv):
    inv.assign(1, "flu-acme", 1)
    inv.robots[2].site, inv.robots[2].slot = "flu-acme", 1
    assert any("вже зайнятий" in e for e in inv.validate())


def test_free_slot_prefers_previous(inv):
    inv.assign(1, "flu-acme", 1)
    assert inv.free_slot("flu-acme") == 2
    assert inv.free_slot("flu-acme", prefer=5) == 5
    assert inv.free_slot("flu-acme", prefer=1) == 2  # зайнятий — беремо перший вільний


def test_full_site_raises(inv):
    for slot in range(1, 5):
        inv.robots[100 + slot] = Robot(id=100 + slot, platform="flu-v1", site="initech", slot=slot)
    with pytest.raises(InventoryError, match="немає вільних місць"):
        inv.free_slot("initech")


def test_assign_keeps_comments_and_puts_slot_after_site(root, inv):
    inv.assign(1, "flu-acme", 7)
    inv.save_robots()
    text = (root / "inventory" / "robots.yaml").read_text("utf-8")
    assert "глобальний номер НРК" in text  # коментар шапки на місці
    block = text.split("- id: 1", 1)[1].split("- id: 2", 1)[0]
    assert block.index("site: flu-acme") < block.index("slot: 7") < block.index("service_tunnel")
    assert Inventory.load(root).robot(1).slot == 7


def test_empty_robot_list(root):
    (root / "inventory" / "robots.yaml").write_text("# порожньо\nrobots:\n", "utf-8")
    inv = Inventory.load(root)
    assert inv.robots == {}


def test_inventory_created_from_templates(tmp_path):
    """Новий ноутбук: inventory/ з'являється з шаблонів; уже наявний свій інвентар не перезаписується."""
    for name in ("inventory.example", "base"):
        shutil.copytree(REPO / name, tmp_path / name)
    inv = Inventory.load(tmp_path)
    assert sorted(p.name for p in (tmp_path / "inventory").iterdir()) == ["operators.yaml", "robots.yaml", "sites.yaml"]
    assert (inv.robots, inv.operators) == ({}, {})
    (tmp_path / "inventory" / "robots.yaml").write_text("robots:\n  - {id: 1, serial: MY-1, platform: flu-v1}\n", "utf-8")
    assert Inventory.load(tmp_path).robot(1).serial == "MY-1"
