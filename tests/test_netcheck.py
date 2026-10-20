"""Перевірка кабелю до роутера: що бачить людина в кожному випадку."""
import pytest

from fleet.i18n import set_lang
from fleet.netcheck import check_router_link


def eth(up=True, dhcp=True, ips=()):
    return {"name": "Ethernet", "up": up, "dhcp": dhcp, "ips": list(ips)}


@pytest.mark.parametrize("adapters, status, text", [
    ([], "no_adapter", "немає порту Ethernet"),
    ([eth(up=False)], "no_cable", "кабель не під'єднано"),
    ([eth(ips=["192.168.88.254"])], "ok", "192.168.88.254"),
    ([eth(dhcp=False, ips=["192.168.50.100", "192.168.1.100"])], "static", "192.168.50.100, 192.168.1.100"),
    ([eth(ips=["169.254.10.20"])], "waiting", "зачекайте 30 секунд"),
    # другий адаптер (USB-Ethernet) із правильною адресою важить більше за вбудований без кабелю
    ([eth(up=False), {**eth(ips=["192.168.88.100"]), "name": "Ethernet 2"}], "ok", "192.168.88.100"),
])
def test_cable_states(adapters, status, text):
    result = check_router_link(adapters)
    assert result["status"] == status
    assert text in result["message"].lower() or text in result["message"]


def test_static_message_in_english():
    set_lang("en")
    message = check_router_link([eth(dhcp=False, ips=["10.0.0.5"])])["message"]
    assert "Obtain an IP address automatically" in message and "192.168.88.250" in message
