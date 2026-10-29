"""Поле 29.10, НРК pg0505 (слот 3): «прошито ✓», а на зв'язок не виходить. Справжня прошивка проти моделі стенда
(field_sim.py) у кожному стані, в якому НРК бував, — і що з того вийшло б, коли Starlink нарешті в порту 1.

Та сама модель на 0.3.3 дає «готово» й «без зв'язку» в A, B, B2, E, E2 і «не повернувся за 300 с» у C — як і було.
"""
from pathlib import Path

import pytest

from tests import field_sim

REPO = Path(__file__).resolve().parents[1]
STOPPED = "не прошито: бракує пристроїв"
STARLINK_PORT = "роутера — мережа Starlink"

NOT_CHECKED = "готово — зв'язок із сервером не перевірено"  # Starlink у порту 1 немає: перевірить крок 6
OFFLINE = "прошито, але НРК не на зв'язку — УВАГА: НРК НЕ НА ЗВ'ЯЗКУ"

EXPECTED = {
    "A": (STARLINK_PORT, "не прошито"),
    "B": (STOPPED, "не прошито"),
    "B2": (NOT_CHECKED, "на зв'язку"),
    "C": (STARLINK_PORT, "не прошито"),
    "D": (STARLINK_PORT, "не прошито"),
    "E": (STOPPED, "не прошито"),
    "E2": (NOT_CHECKED, "на зв'язку"),
    "F": (NOT_CHECKED, "на зв'язку"),
    "G": ("готово — зв'язок із сервером є", "на зв'язку"),
    # поле 26.10 (чат MTU 1280): адаптер ФК переїхав з порту камери — дубль оренди обривав конфіг RouterOS
    "J": (STOPPED, "не прошито"),
    "J2": (NOT_CHECKED, "на зв'язку"),
    "H": (OFFLINE + ". Starlink у порту 1 дав адресу 192.168.1.37/24, але інтернету немає",
          "без зв'язку: у Starlink немає інтернету"),
    "I": (OFFLINE + ". Інтернет є, а сервер 13.63.179.54", "без зв'язку: сервер не знає цього НРК"),
}


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    return {r["key"]: r for r in field_sim.run_all(tmp_path_factory.mktemp("field"), REPO)}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_field_state(rows, key):
    row, (said, outcome) = rows[key], EXPECTED[key]
    assert said in row["said"] and row["outcome"] == outcome, (row["title"], row["said"], row["result"]["log"])
    if row["result"].get("rsc"):
        assert "на 192.168.88.1 і скидаю роутер" in row["result"]["log"]  # залито на роутер, а не «на cam2»


def test_port_of_the_starlink_is_named(rows):
    assert "у порту 3 (ether3)" in rows["A"]["said"] and "у порту 5 (ether5)" in rows["C"]["said"]


def test_phone_is_forgotten_and_the_real_camera_kept(rows):
    """B: телефон із минулої прошивки забуто й у robots.yaml; F: справжня задня камера записалася замість нього."""
    assert "192.168.1.68" not in (rows["B"]["root"] / "inventory" / "robots.yaml").read_text("utf-8")
    assert "з минулої прошивки — випадкова MAC" in rows["B"]["result"]["log"]
    robots = (rows["F"]["root"] / "inventory" / "robots.yaml").read_text("utf-8")
    assert "192.168.15.109" in robots and "192.168.1.68" not in robots


def test_moved_device_is_not_kept_twice(rows):
    """J: старий запис «задня камера = адаптер ФК» забуто — інакше дві оренди з однією MAC, і скрипт обривається."""
    assert "задня камера: 192.168.15.253 (3C:AB:72:52:62:BA) з минулої прошивки — тепер це ФК" in rows["J"]["result"]["log"]
    assert field_sim.dup_lease(rows["J2"]["result"]["rsc"]) is None


def test_camera_on_the_factory_address_is_explained(rows):
    assert "192.168.1.108 (8C:E9:B4:6B:D9:A9) — це мережа Starlink" in rows["E"]["result"]["log"]
