"""Какие сообщения наземной станции пропускать к роботу.

Владелец робота может всё. Остальным подключённым станциям можно только читать
(параметры, миссию, телеметрию) и, если разрешено, перевести робота в Hold.
Heartbeat не-владельца не пропускается никогда: иначе он держал бы робота
«на связи», и GCS-failsafe не сработал бы при обрыве связи у владельца.
"""
from __future__ import annotations

from dataclasses import dataclass

from pymavlink.dialects.v20 import ardupilotmega as mavlink

READ_ONLY_MESSAGES = {
    "PARAM_REQUEST_LIST",
    "PARAM_REQUEST_READ",
    "MISSION_REQUEST_LIST",
    "MISSION_REQUEST",
    "MISSION_REQUEST_INT",
    "REQUEST_DATA_STREAM",
    "TIMESYNC",
    "SYSTEM_TIME",
    "PING",
    "AUTOPILOT_VERSION_REQUEST",
}

READ_ONLY_COMMANDS = {
    mavlink.MAV_CMD_REQUEST_MESSAGE,
    mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
    mavlink.MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES,
    mavlink.MAV_CMD_REQUEST_PROTOCOL_VERSION,
}

# MAVLink FTP (им наземки качают параметры): чтение разрешаем, запись — нет.
# 0 None, 1 TerminateSession, 2 ResetSessions, 3 ListDirectory, 4 OpenFileRO,
# 5 ReadFile, 14 CalcFileCRC32, 15 BurstReadFile
FTP_READ_OPCODES = {0, 1, 2, 3, 4, 5, 14, 15}
FTP_OPCODE_OFFSET = 3


@dataclass
class Policy:
    stop_from_any: bool = True  # любой оператор площадки может перевести робота в Hold
    hold_on_handover: bool = True  # при передаче робот сначала встаёт в Hold
    handover_timeout_s: int = 120  # сколько ждать, пока получатель примет робота


def classify(msg, hold_mode: int) -> str:
    """heartbeat | read | stop | control"""
    t = msg.get_type()
    if t == "HEARTBEAT":
        return "heartbeat"
    if t in READ_ONLY_MESSAGES:
        return "read"
    if t in ("COMMAND_LONG", "COMMAND_INT"):
        if msg.command in READ_ONLY_COMMANDS:
            return "read"
        if (
            msg.command == mavlink.MAV_CMD_DO_SET_MODE
            and int(msg.param1) & mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            and int(msg.param2) == hold_mode
        ):
            return "stop"
        return "control"
    if t == "SET_MODE" and msg.custom_mode == hold_mode:
        return "stop"
    if t == "FILE_TRANSFER_PROTOCOL":
        payload = msg.payload
        if len(payload) > FTP_OPCODE_OFFSET and payload[FTP_OPCODE_OFFSET] in FTP_READ_OPCODES:
            return "read"
        return "control"
    return "control"


def allowed(msg, is_owner: bool, policy: Policy, hold_mode: int) -> bool:
    if is_owner:
        return True
    kind = classify(msg, hold_mode)
    return kind == "read" or (kind == "stop" and policy.stop_from_any)
