"""Арбитр: правила пропуска сообщений и живой прогон с фейковым роботом и тремя операторами."""
import asyncio
import json

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

import arbiter as arb_mod
from policy import Policy, allowed, classify

HOLD = 4


def gcs() -> mavlink.MAVLink:
    return mavlink.MAVLink(None, srcSystem=255, srcComponent=190)


def heartbeat(m):
    return m.heartbeat_encode(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)


def arm(m, target=7):
    return m.command_long_encode(target, 1, mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)


def set_mode(m, mode, target=7):
    return m.command_long_encode(target, 1, mavlink.MAV_CMD_DO_SET_MODE, 0,
                                 mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode, 0, 0, 0, 0, 0)


def ftp(m, opcode):
    payload = [0] * 251
    payload[3] = opcode
    return m.file_transfer_protocol_encode(0, 7, 1, payload)


# --- правила -------------------------------------------------------------------

def test_policy_classification():
    m = gcs()
    assert classify(heartbeat(m), HOLD) == "heartbeat"
    assert classify(m.param_request_list_encode(7, 1), HOLD) == "read"
    assert classify(m.param_set_encode(7, 1, b"FS_ACTION", 0, 9), HOLD) == "control"
    assert classify(arm(m), HOLD) == "control"
    assert classify(set_mode(m, HOLD), HOLD) == "stop"
    assert classify(set_mode(m, 10), HOLD) == "control"  # AUTO — это не стоп
    assert classify(m.set_mode_encode(7, 1, HOLD), HOLD) == "stop"
    assert classify(ftp(m, 5), HOLD) == "read"  # ReadFile
    assert classify(ftp(m, 7), HOLD) == "control"  # WriteFile


def test_non_owner_rights():
    m, p = gcs(), Policy()
    assert allowed(arm(m), True, p, HOLD)
    assert not allowed(arm(m), False, p, HOLD)
    assert not allowed(heartbeat(m), False, p, HOLD)  # иначе failsafe не заметит пропажу владельца
    assert allowed(m.param_request_list_encode(7, 1), False, p, HOLD)
    assert allowed(set_mode(m, HOLD), False, p, HOLD)
    assert not allowed(set_mode(m, HOLD), False, Policy(stop_from_any=False), HOLD)


# --- живой прогон --------------------------------------------------------------

class FakeRobot:
    """TCP-сервер вместо адаптера ФК: шлёт heartbeat SYSID 7 и запоминает, что получил."""

    def __init__(self):
        self.received: list = []
        self.mav = mavlink.MAVLink(None, srcSystem=7, srcComponent=1)

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        parser = arb_mod.new_parser()

        async def beat():
            while True:
                hb = self.mav.heartbeat_encode(mavlink.MAV_TYPE_GROUND_ROVER, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 0)
                writer.write(hb.pack(self.mav))
                await asyncio.sleep(0.1)

        task = asyncio.create_task(beat())
        try:
            while data := await reader.read(4096):
                self.received += [m for m in parser.parse_buffer(data) or [] if m.get_type() != "BAD_DATA"]
        finally:
            task.cancel()

    def types(self) -> list[str]:
        return [m.get_type() for m in self.received]


class Station:
    """Наземная станция с заданным адресом источника (оператор определяется по IP)."""

    def __init__(self, ip: str):
        self.ip = ip
        self.mav = gcs()
        self.received: list = []

    async def connect(self, port: int):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(self.ip, 0))
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        parser = arb_mod.new_parser()
        while data := await self.reader.read(4096):
            self.received += parser.parse_buffer(data) or []

    def send(self, msg):
        self.writer.write(msg.pack(self.mav))

    def close(self):
        self.task.cancel()
        self.writer.close()


async def settle(seconds=0.3):
    await asyncio.sleep(seconds)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def scenario(tmp_path):
    robot = FakeRobot()
    robot_port = await robot.start()
    listen = free_port()
    config = tmp_path / "arbiter.yaml"
    config.write_text(json.dumps({
        "site": "test",
        "bind": "127.0.0.1",
        "robots": [{"name": "r1", "slot": 7, "host": "127.0.0.1", "port": robot_port, "listen": listen, "hold_mode": HOLD}],
        "operators": [
            {"ip": "127.0.0.2", "name": "a", "role": "operator"},
            {"ip": "127.0.0.3", "name": "b", "role": "operator"},
            {"ip": "127.0.0.4", "name": "d", "role": "dispatcher"},
        ],
    }), "utf-8")
    arb = arb_mod.Arbiter(config, tmp_path / "state")
    await arb.load_config()
    link = arb.links["r1"]
    ops = {o["name"]: o for o in arb.operators.values()}

    a, b, stranger = Station("127.0.0.2"), Station("127.0.0.3"), Station("127.0.0.9")
    await a.connect(listen)
    await b.connect(listen)
    await stranger.connect(listen)
    await settle(0.6)

    # телеметрия робота доходит до всех операторов, чужой адрес отключён
    assert link.robot_writer is not None and link.robot_sysid == 7
    assert any(m.get_type() == "HEARTBEAT" for m in a.received)
    assert any(m.get_type() == "HEARTBEAT" for m in b.received)
    assert not stranger.received

    # пока владельца нет: чтение проходит, heartbeat и управление — нет
    a.send(heartbeat(a.mav))
    a.send(arm(a.mav))
    a.send(a.mav.param_request_list_encode(7, 1))
    await settle()
    assert robot.types() == ["PARAM_REQUEST_LIST"]

    # a берёт робота: теперь его heartbeat и команды проходят, а b — нет
    assert arb.act(ops["a"], "r1", "claim", {})[0] == 200
    assert arb.act(ops["b"], "r1", "claim", {})[0] == 409
    robot.received.clear()
    a.send(heartbeat(a.mav))
    a.send(arm(a.mav))
    b.send(heartbeat(b.mav))
    b.send(arm(b.mav))
    await settle()
    assert robot.types() == ["HEARTBEAT", "COMMAND_LONG"]

    # «стоп» может нажать любой оператор площадки
    robot.received.clear()
    b.send(set_mode(b.mav, HOLD))
    await settle()
    assert robot.types() == ["COMMAND_LONG"] and robot.received[0].param2 == HOLD

    # передача a -> b: арбитр сам ставит Hold, b принимает
    robot.received.clear()
    assert arb.act(ops["b"], "r1", "accept", {})[0] == 409  # ещё не предлагали
    assert arb.act(ops["a"], "r1", "handover", {"to": "b"})[0] == 200
    await settle()
    hold = [m for m in robot.received if m.get_type() == "COMMAND_LONG"]
    assert hold and hold[0].command == mavlink.MAV_CMD_DO_SET_MODE and hold[0].param2 == HOLD and hold[0].target_system == 7
    assert arb.act(ops["b"], "r1", "accept", {})[0] == 200
    assert arb.journal.owner("r1") == "b"

    robot.received.clear()
    a.send(heartbeat(a.mav))
    b.send(heartbeat(b.mav))
    await settle()
    assert len(robot.received) == 1  # только heartbeat нового владельца

    # оператор не может забрать робота силой, диспетчер — может
    assert arb.act(ops["a"], "r1", "takeover", {})[0] == 403
    assert arb.act(ops["d"], "r1", "takeover", {})[0] == 200
    assert arb.journal.owner("r1") == "d"

    events = [json.loads(line)["event"] for line in (tmp_path / "state" / "audit.jsonl").read_text("utf-8").splitlines()]
    for event in ("claim", "hold", "handover_offer", "handover", "takeover"):
        assert event in events
    # владелец переживает перезапуск арбитра
    assert json.loads((tmp_path / "state" / "owners.json").read_text("utf-8")) == {"r1": "d"}

    # конфиг перечитывается на лету: оператора b убрали — его соединение закрыто
    cfg = json.loads(config.read_text("utf-8"))
    cfg["operators"] = [o for o in cfg["operators"] if o["name"] != "b"]
    config.write_text(json.dumps(cfg), "utf-8")
    await arb.load_config()
    await settle()
    assert {c.name for c in link.clients} == {"a"}

    for st in (a, b, stranger):
        st.close()
    for lnk in list(arb.links.values()):
        await lnk.stop()
    robot.server.close()


def loopback_alias_available() -> bool:
    import socket

    try:
        with socket.socket() as s:
            s.bind(("127.0.0.2", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not loopback_alias_available(), reason="нет адресов 127.0.0.x кроме 127.0.0.1")
def test_arbiter_end_to_end(tmp_path):
    asyncio.run(asyncio.wait_for(scenario(tmp_path), timeout=30))


def test_http_api(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        config = tmp_path / "arbiter.yaml"
        config.write_text(json.dumps({
            "site": "test", "bind": "127.0.0.1",
            "robots": [{"name": "r1", "slot": 7, "host": "127.0.0.1", "port": 9, "listen": free_port(), "hold_mode": HOLD}],
            "operators": [{"ip": "127.0.0.1", "name": "a", "role": "operator"}],
        }), "utf-8")
        arb = arb_mod.Arbiter(config, tmp_path / "state")
        await arb.load_config()
        async with TestClient(TestServer(arb_mod.make_app(arb))) as client:
            state = await (await client.get("/api/state")).json()
            assert state["me"]["name"] == "a" and state["robots"][0]["name"] == "r1"
            r = await client.post("/api/robots/r1/claim")
            assert r.status == 403  # без X-Fleet: защита от запросов с чужих страниц
            r = await client.post("/api/robots/r1/claim", headers={"X-Fleet": "1"})
            assert r.status == 200 and (await r.json())["ok"]
            assert (await client.get("/")).status == 200
        for lnk in list(arb.links.values()):
            await lnk.stop()

    asyncio.run(run())
