"""Полётный контроллер: чтение, сравнение и заливка параметров по MAVLink.

Подключение — USB на стенде (COM5, /dev/ttyACM0) или TCP через арбитр площадки
(tcp:10.150.0.1:5761). Через арбитр менять параметры может только владелец
робота: возьмите робота в панели, иначе запись молча отбрасывается.
"""
from __future__ import annotations

import time

from pymavlink import mavutil

GCS_SYSID = 255
GCS_COMPID = 190


class FcError(Exception):
    pass


def _param_id(msg) -> str:
    pid = msg.param_id
    if isinstance(pid, bytes):
        pid = pid.decode(errors="replace")
    return pid.split("\x00", 1)[0]


def connect(conn: str, baud: int = 115200, timeout: float = 15):
    """Подключиться и дождаться heartbeat автопилота (не камеры, не другой станции)."""
    m = mavutil.mavlink_connection(conn, baud=baud, source_system=GCS_SYSID, source_component=GCS_COMPID)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hb = m.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if hb and hb.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID:
            m.target_system, m.target_component = hb.get_srcSystem(), hb.get_srcComponent()
            return m, hb
    m.close()
    raise FcError(f"нет heartbeat автопилота на {conn} за {int(timeout)} с")


def fetch_params(m, timeout: float = 90) -> tuple[dict[str, float], dict[str, int]]:
    """Все параметры: значения и типы. Потерянные дозапрашиваются по индексу."""
    values: dict[str, float] = {}
    types: dict[str, int] = {}
    seen: set[int] = set()
    total: int | None = None
    m.mav.param_request_list_send(m.target_system, m.target_component)
    deadline = time.monotonic() + timeout
    last_rx = time.monotonic()
    while time.monotonic() < deadline:
        msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=1)
        if msg is not None and msg.get_srcSystem() == m.target_system:
            name = _param_id(msg)
            values[name], types[name] = msg.param_value, msg.param_type
            total = msg.param_count
            if 0 <= msg.param_index < 65535:
                seen.add(msg.param_index)
            last_rx = time.monotonic()
            if len(seen) >= total:
                return values, types
        elif total is not None and time.monotonic() - last_rx > 2:
            for idx in sorted(set(range(total)) - seen)[:20]:
                m.mav.param_request_read_send(m.target_system, m.target_component, b"", idx)
            last_rx = time.monotonic()
    got = f"{len(seen)} из {total}" if total else "ни одного"
    raise FcError(f"параметры получены не полностью ({got}) за {int(timeout)} с")


def set_param(m, name: str, value: float, ptype: int, retries: int = 3) -> float | None:
    """Записать параметр и дождаться подтверждения. None — подтверждения нет."""
    for _ in range(retries):
        m.mav.param_set_send(m.target_system, m.target_component, name.encode(), float(value), ptype)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if msg is not None and _param_id(msg) == name:
                return msg.param_value
    return None


def reboot(m) -> None:
    m.mav.command_long_send(
        m.target_system, m.target_component,
        mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 0, 1, 0, 0, 0, 0, 0, 0,
    )


def telemetry(m, seconds: float = 8) -> dict:
    """Короткий срез телеметрии для приёмочного теста."""
    m.mav.request_data_stream_send(m.target_system, m.target_component, mavutil.mavlink.MAV_DATA_STREAM_ALL, 2, 1)
    out: dict = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        msg = m.recv_match(type=["GPS_RAW_INT", "SYS_STATUS"], blocking=True, timeout=1)
        if msg is None or msg.get_srcSystem() != m.target_system:
            continue
        if msg.get_type() == "GPS_RAW_INT":
            out["gps_fix"] = msg.fix_type
            out["satellites"] = msg.satellites_visible
        else:
            out["battery_v"] = msg.voltage_battery / 1000 if msg.voltage_battery != 65535 else None
        if "gps_fix" in out and "battery_v" in out:
            break
    return out
