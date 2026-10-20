#!/usr/bin/env python3
"""Арбитр управления MAVLink для площадки FLU.

Для каждого робота арбитр держит одно TCP-соединение с полётным контроллером
и открывает TCP-порт для наземных станций (QGroundControl, Mission Planner):

* телеметрия робота уходит всем подключённым станциям;
* команды проходят только от владельца робота (остальным — чтение и «стоп»);
* heartbeat к роботу пропускается только от владельца, поэтому GCS-failsafe
  робота следит именно за ним: пропал владелец — робот встал.

Оператор определяется по адресу в туннеле WireGuard — подделать его нельзя.
Взять, передать и забрать робота можно в веб-панели; всё пишется в журнал.
Конфиг перечитывается на лету: деплой не рвёт соединения операторов.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import functools
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from aiohttp import web
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from policy import Policy, allowed, classify

log = logging.getLogger("arbiter")

GCS_SYSID = 255
GCS_COMPID = mavlink.MAV_COMP_ID_MISSIONPLANNER
CLIENT_BUFFER_LIMIT = 1 << 20
ROBOT_IDLE_TIMEOUT_S = 15  # робот молчит дольше — переподключаемся
RECONNECT_DELAY_S = 3
CONFIG_POLL_S = 5
PANEL = Path(__file__).with_name("panel.html")
json_dumps = functools.partial(json.dumps, ensure_ascii=False)


def new_parser() -> mavlink.MAVLink:
    parser = mavlink.MAVLink(None)
    parser.robust_parsing = True
    return parser


@dataclass(eq=False)
class Client:
    name: str
    role: str
    writer: asyncio.StreamWriter
    parser: mavlink.MAVLink = field(default_factory=new_parser)
    denied: int = 0
    dropped: int = 0

    def send(self, buf: bytes) -> None:
        if self.writer.is_closing():
            return
        if self.writer.transport.get_write_buffer_size() > CLIENT_BUFFER_LIMIT:
            self.dropped += 1  # медленный клиент: телеметрия всё равно устареет, не копим
            return
        self.writer.write(buf)


class Journal:
    """Владельцы роботов (переживают перезапуск) и журнал событий."""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.owners_path = state_dir / "owners.json"
        self.audit_path = state_dir / "audit.jsonl"
        self.owners: dict[str, str] = {}
        if self.owners_path.exists():
            self.owners = json.loads(self.owners_path.read_text("utf-8"))

    def owner(self, robot: str) -> str | None:
        return self.owners.get(robot)

    def set_owner(self, robot: str, name: str | None) -> None:
        if name is None:
            self.owners.pop(robot, None)
        else:
            self.owners[robot] = name
        tmp = self.owners_path.with_suffix(".tmp")
        tmp.write_text(json_dumps(self.owners, indent=1), "utf-8")
        tmp.replace(self.owners_path)

    def event(self, robot: str, event: str, **fields) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "robot": robot, "event": event, **fields}
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json_dumps(record) + "\n")
        log.info("%s: %s %s", robot, event, fields)

    def recent(self, n: int = 30) -> list[dict]:
        if not self.audit_path.exists():
            return []
        with self.audit_path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in collections.deque(f, maxlen=n)][::-1]


class RobotLink:
    """Один робот: соединение с ФК и порт для наземных станций."""

    def __init__(self, arb: Arbiter, cfg: dict):
        self.arb = arb
        self.cfg = cfg
        self.name: str = cfg["name"]
        self.clients: set[Client] = set()
        self.robot_writer: asyncio.StreamWriter | None = None
        self.robot_hb = 0.0
        self.robot_sysid: int | None = None  # фактический SYSID: после переезда может ещё не совпадать со slot
        self.owner_hb = 0.0
        self.ftp_client: Client | None = None
        self.pending: dict | None = None  # передача: {"to", "by", "since"}
        self.server: asyncio.AbstractServer | None = None
        self.task: asyncio.Task | None = None
        self.injector = mavlink.MAVLink(None, srcSystem=GCS_SYSID, srcComponent=GCS_COMPID)

    async def start(self, bind: str) -> None:
        self.server = await asyncio.start_server(self.handle_client, bind, self.cfg["listen"])
        self.task = asyncio.create_task(self.robot_loop())
        log.info("%s: станции -> %s:%s, робот %s:%s", self.name, bind, self.cfg["listen"], self.cfg["host"], self.cfg["port"])

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.server:
            self.server.close()
        for client in list(self.clients):
            client.writer.close()
        if self.robot_writer:
            self.robot_writer.close()

    # --- робот ---------------------------------------------------------------

    async def robot_loop(self) -> None:
        host, port = self.cfg["host"], self.cfg["port"]
        while True:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue
            log.info("%s: робот на связи", self.name)
            self.robot_writer = writer
            parser = new_parser()
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(65536), timeout=ROBOT_IDLE_TIMEOUT_S)
                    if not data:
                        break
                    self.from_robot(parser, data)
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                self.robot_writer = None
                writer.close()
                log.warning("%s: связь с роботом потеряна", self.name)
            await asyncio.sleep(1)

    def from_robot(self, parser: mavlink.MAVLink, data: bytes) -> None:
        for msg in parser.parse_buffer(data) or []:
            t = msg.get_type()
            if t == "BAD_DATA":
                continue
            if t == "HEARTBEAT" and msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID:
                self.robot_hb = time.monotonic()
                self.robot_sysid = msg.get_srcSystem()
            buf = msg.get_msgbuf()
            if t == "FILE_TRANSFER_PROTOCOL" and self.ftp_client in self.clients:
                self.ftp_client.send(buf)  # ответ FTP — только тому, кто спрашивал
                continue
            for client in list(self.clients):
                client.send(buf)

    def to_robot(self, buf: bytes) -> None:
        if self.robot_writer and not self.robot_writer.is_closing():
            self.robot_writer.write(buf)

    def send_hold(self) -> None:
        msg = self.injector.command_long_encode(
            self.robot_sysid or self.cfg["slot"], 1, mavlink.MAV_CMD_DO_SET_MODE, 0,
            mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, self.cfg["hold_mode"], 0, 0, 0, 0, 0,
        )
        self.to_robot(msg.pack(self.injector))

    # --- наземные станции ----------------------------------------------------

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = writer.get_extra_info("peername")[0]
        op = self.arb.operators.get(ip)
        if op is None:
            log.warning("%s: отклонено подключение с неизвестного адреса %s", self.name, ip)
            writer.close()
            return
        client = Client(op["name"], op["role"], writer)
        self.clients.add(client)
        self.arb.journal.event(self.name, "connect", by=client.name)
        try:
            while data := await reader.read(65536):
                self.from_client(client, data)
        except OSError:
            pass
        finally:
            self.clients.discard(client)
            if self.ftp_client is client:
                self.ftp_client = None
            writer.close()
            self.arb.journal.event(self.name, "disconnect", by=client.name, denied=client.denied, dropped=client.dropped)

    def from_client(self, client: Client, data: bytes) -> None:
        is_owner = self.arb.journal.owner(self.name) == client.name
        hold_mode = self.cfg["hold_mode"]
        for msg in client.parser.parse_buffer(data) or []:
            t = msg.get_type()
            if t == "BAD_DATA":
                continue
            if not allowed(msg, is_owner, self.arb.policy, hold_mode):
                client.denied += 1
                continue
            if t == "HEARTBEAT":
                self.owner_hb = time.monotonic()
            elif t == "FILE_TRANSFER_PROTOCOL":
                self.ftp_client = client
            if not is_owner and classify(msg, hold_mode) == "stop":
                self.arb.journal.event(self.name, "hold", by=client.name, via="gcs")
            self.to_robot(msg.get_msgbuf())

    # --- состояние -----------------------------------------------------------

    def expire_pending(self) -> None:
        if self.pending and time.monotonic() - self.pending["since"] > self.arb.policy.handover_timeout_s:
            self.arb.journal.event(self.name, "handover_expired", by=self.pending["by"], to=self.pending["to"])
            self.pending = None

    def status(self) -> dict:
        self.expire_pending()
        now = time.monotonic()
        owner = self.arb.journal.owner(self.name)
        return {
            "name": self.name,
            "slot": self.cfg["slot"],
            "port": self.cfg["listen"],
            "robot_online": self.robot_writer is not None,
            "robot_heartbeat_age": round(now - self.robot_hb, 1) if self.robot_hb else None,
            "robot_sysid": self.robot_sysid,
            "owner": owner,
            "owner_connected": any(c.name == owner for c in self.clients),
            "owner_heartbeat_age": round(now - self.owner_hb, 1) if self.owner_hb else None,
            "pending": self.pending and {
                "to": self.pending["to"], "by": self.pending["by"], "age": round(now - self.pending["since"]),
            },
            "clients": sorted({c.name for c in self.clients}),
        }


class Arbiter:
    def __init__(self, config_path: Path, state_dir: Path):
        self.config_path = config_path
        self.journal = Journal(state_dir)
        self.links: dict[str, RobotLink] = {}
        self.operators: dict[str, dict] = {}
        self.policy = Policy()
        self.site = ""
        self.bind = "127.0.0.1"
        self.panel_port = 8080
        self._mtime = 0.0

    async def load_config(self) -> None:
        cfg = yaml.safe_load(self.config_path.read_text("utf-8")) or {}
        self._mtime = self.config_path.stat().st_mtime
        self.site = cfg.get("site", "")
        self.bind = cfg.get("bind", self.bind)
        self.panel_port = cfg.get("panel_port", self.panel_port)
        self.policy = Policy(**(cfg.get("policy") or {}))
        self.operators = {o["ip"]: o for o in cfg.get("operators") or []}

        wanted = {r["name"]: r for r in cfg.get("robots") or []}
        for name in list(self.links):
            if wanted.get(name) != self.links[name].cfg:
                await self.links.pop(name).stop()
                log.info("%s: снят с арбитра", name)
        for name, rcfg in wanted.items():
            if name in self.links:
                continue
            link = RobotLink(self, rcfg)
            try:
                await link.start(self.bind)
            except OSError as exc:
                log.error("%s: не удалось открыть порт %s: %s", name, rcfg["listen"], exc)
                continue
            self.links[name] = link

        # Оператора удалили из конфига — его соединения закрываем
        for link in self.links.values():
            for client in list(link.clients):
                ip = client.writer.get_extra_info("peername")[0]
                if self.operators.get(ip, {}).get("name") != client.name:
                    client.writer.close()

    async def watch_config(self) -> None:
        while True:
            await asyncio.sleep(CONFIG_POLL_S)
            try:
                if self.config_path.stat().st_mtime != self._mtime:
                    log.info("конфиг изменился, перечитываю")
                    await self.load_config()
            except Exception:
                log.exception("не удалось перечитать конфиг, работаю со старым")

    def set_owner(self, link: RobotLink, name: str | None, event: str, **fields) -> None:
        self.journal.set_owner(link.name, name)
        link.owner_hb = 0.0
        self.journal.event(link.name, event, **fields)

    def act(self, op: dict, robot: str, action: str, body: dict) -> tuple[int, str]:
        link = self.links.get(robot)
        if link is None:
            return 404, "нет такого робота"
        link.expire_pending()
        me, owner = op["name"], self.journal.owner(robot)
        hold = self.policy.hold_on_handover

        if action == "claim":
            if owner == me:
                return 200, "робот уже ваш"
            if owner is not None:
                return 409, f"робот занят: владелец {owner}. Попросите его передать робота или обратитесь к диспетчеру"
            self.set_owner(link, me, "claim", by=me)
            return 200, "робот ваш"

        if action == "release":
            if owner != me:
                return 403, "освободить робота может только владелец"
            if hold:
                link.send_hold()
            link.pending = None
            self.set_owner(link, None, "release", by=me, hold=hold)
            return 200, "робот свободен"

        if action == "handover":
            if owner != me:
                return 403, "передать робота может только владелец"
            to = str(body.get("to", ""))
            if to == me or to not in {o["name"] for o in self.operators.values()}:
                return 400, "выберите другого оператора площадки"
            link.pending = {"to": to, "by": me, "since": time.monotonic()}
            if hold:
                link.send_hold()
            self.journal.event(robot, "handover_offer", by=me, to=to, hold=hold)
            return 200, f"ждём, пока {to} примет робота"

        if action == "accept":
            if not link.pending or link.pending["to"] != me:
                return 409, "вам этого робота не передавали"
            if not any(c.name == me for c in link.clients):
                return 409, "сначала подключитесь к роботу в наземной станции: так видно, что связь у вас есть"
            prev = link.pending["by"]
            link.pending = None
            self.set_owner(link, me, "handover", by=prev, to=me)
            return 200, "робот ваш"

        if action == "cancel":
            if not link.pending or me not in (link.pending["to"], link.pending["by"]):
                return 409, "нет передачи, которую вы можете отменить"
            self.journal.event(robot, "handover_cancel", by=me)
            link.pending = None
            return 200, "передача отменена"

        if action == "takeover":
            if op["role"] != "dispatcher":
                return 403, "принудительно забрать робота может только диспетчер"
            if hold:
                link.send_hold()
            link.pending = None
            self.set_owner(link, me, "takeover", by=me, previous=owner, hold=hold)
            return 200, "робот ваш (принудительно)"

        if action == "hold":
            if owner != me and not self.policy.stop_from_any:
                return 403, "остановить робота может только владелец"
            link.send_hold()
            self.journal.event(robot, "hold", by=me, via="panel")
            return 200, "команда Hold отправлена"

        return 400, "неизвестное действие"


def make_app(arb: Arbiter) -> web.Application:
    routes = web.RouteTableDef()

    def operator(request: web.Request) -> dict:
        op = arb.operators.get(request.remote)
        if op is None:
            raise web.HTTPForbidden(text="неизвестный адрес: подключитесь через WireGuard площадки")
        return op

    @routes.get("/")
    async def index(request: web.Request) -> web.StreamResponse:
        operator(request)
        return web.FileResponse(PANEL)

    @routes.get("/api/state")
    async def state(request: web.Request) -> web.Response:
        me = operator(request)
        links = sorted(arb.links.values(), key=lambda link: link.cfg["slot"])
        return web.json_response({
            "site": arb.site,
            "me": me,
            "operators": sorted(o["name"] for o in arb.operators.values()),
            "stop_from_any": arb.policy.stop_from_any,
            "robots": [link.status() for link in links],
        }, dumps=json_dumps)

    @routes.get("/api/journal")
    async def journal(request: web.Request) -> web.Response:
        operator(request)
        return web.json_response(arb.journal.recent(), dumps=json_dumps)

    @routes.post("/api/robots/{name}/{action}")
    async def action(request: web.Request) -> web.Response:
        op = operator(request)
        # Свой заголовок: чужая страница в браузере оператора не сможет
        # отправить такой запрос без CORS, а CORS мы не разрешаем.
        if request.headers.get("X-Fleet") != "1":
            raise web.HTTPForbidden(text="нет заголовка X-Fleet")
        try:
            body = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            body = {}
        code, message = arb.act(op, request.match_info["name"], request.match_info["action"], body or {})
        return web.json_response({"ok": code == 200, "message": message}, status=code, dumps=json_dumps)

    app = web.Application()
    app.add_routes(routes)
    return app


async def run(config: Path, state: Path) -> None:
    arb = Arbiter(config, state)
    await arb.load_config()
    runner = web.AppRunner(make_app(arb))
    await runner.setup()
    await web.TCPSite(runner, arb.bind, arb.panel_port).start()
    log.info("панель: http://%s:%s", arb.bind, arb.panel_port)
    await arb.watch_config()


def main() -> None:
    p = argparse.ArgumentParser(description="Арбитр управления MAVLink площадки FLU")
    p.add_argument("--config", default="/config/arbiter.yaml")
    p.add_argument("--state", default="/state")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run(Path(args.config), Path(args.state)))


if __name__ == "__main__":
    main()
