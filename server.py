import asyncio
import json
import math
import os
import random
from aiohttp import web

rooms = {}  # room_code -> {"clients": set(), "moves": [], "colors": {}}
pong_rooms = {}  # room_code -> PongRoom

PONG_WIDTH = 2000
PONG_HEIGHT = 600
PADDLE_WIDTH = 12
PADDLE_HEIGHT = 94
PADDLE_LEFT_X = 30
PADDLE_RIGHT_X = PONG_WIDTH - 30 - PADDLE_WIDTH
BALL_RADIUS = 8
WINNING_SCORE = 7
TICK_INTERVAL = 0.04  # 25 Hz server tick


class PongRoom:
    def __init__(self, room_code: str):
        self.room_code = room_code
        self.clients: set[web.WebSocketResponse] = set()
        self.sides: dict[web.WebSocketResponse, str] = {}
        self.task: asyncio.Task | None = None
        self.reset_match_state(keep_scores=False)

    def reset_match_state(self, keep_scores: bool = False):
        if not keep_scores:
            self.player_score = 0
            self.cpu_score = 0
        self.py = PONG_HEIGHT / 2 - PADDLE_HEIGHT / 2
        self.cy = PONG_HEIGHT / 2 - PADDLE_HEIGHT / 2
        self.bx = PONG_WIDTH / 2
        self.by = PONG_HEIGHT / 2
        self.vx = 0.0
        self.vy = 0.0
        self.speed = 430.0
        self.point_pause = 0.0
        self.next_direction = 1
        self.paused = True
        self.game_over = False

    def count_players(self) -> int:
        return sum(1 for s in self.sides.values() if s in ("left", "right"))

    def is_ready(self) -> bool:
        return self.count_players() >= 2

    def snapshot(self) -> dict:
        return {
            "py": self.py,
            "cy": self.cy,
            "bx": self.bx,
            "by": self.by,
            "vx": self.vx,
            "vy": self.vy,
            "speed": self.speed,
            "playerScore": self.player_score,
            "cpuScore": self.cpu_score,
            "pointPause": self.point_pause,
            "nextDirection": self.next_direction,
            "gameOver": self.game_over,
        }

    def serve(self, direction: int | None = None):
        if not self.is_ready():
            return
        if direction is None:
            direction = -1 if random.random() < 0.5 else 1
        self.bx = PONG_WIDTH / 2
        self.by = PONG_HEIGHT / 2
        self.speed = 430.0
        angle = random.uniform(-0.36, 0.36)
        self.vx = math.cos(angle) * self.speed * direction
        self.vy = math.sin(angle) * self.speed
        self.point_pause = 0.0
        self.paused = False
        self.game_over = False

    def _hit_paddle(self, paddle_y: float, direction: int, sound_out: list):
        rel = (self.by - (paddle_y + PADDLE_HEIGHT / 2)) / (PADDLE_HEIGHT / 2)
        rel = max(-1.0, min(1.0, rel))
        angle = rel * 0.95
        self.speed = min(self.speed + 24.0, 700.0)
        self.vx = math.cos(angle) * self.speed * direction
        self.vy = math.sin(angle) * self.speed
        if direction > 0:
            self.bx = PADDLE_LEFT_X + PADDLE_WIDTH + BALL_RADIUS
        else:
            self.bx = PADDLE_RIGHT_X - BALL_RADIUS
        freq = 420.0 + abs(rel) * 180.0
        sound_out.append([freq, 0.04, "square"])

    def _score_point(self, left_won: bool, sound_out: list):
        if left_won:
            self.player_score += 1
        else:
            self.cpu_score += 1

        sound_out.append([520 if left_won else 300, 0.12, "sine"])

        if self.player_score >= WINNING_SCORE or self.cpu_score >= WINNING_SCORE:
            self.game_over = True
            self.paused = True
            self.vx = 0.0
            self.vy = 0.0
            sound_out.append([660 if self.player_score >= WINNING_SCORE else 210, 0.22, "sine"])
            return

        self.point_pause = 0.85
        self.bx = PONG_WIDTH / 2
        self.by = PONG_HEIGHT / 2
        self.vx = 0.0
        self.vy = 0.0
        self.next_direction = 1 if left_won else -1

    def step(self, delta: float) -> tuple[bool, list]:
        sounds: list = []
        if not self.is_ready():
            return False, sounds

        if self.point_pause > 0:
            self.point_pause -= delta
            if self.point_pause <= 0:
                self.serve(self.next_direction)
            return True, sounds

        if self.paused or self.game_over:
            return False, sounds

        self.bx += self.vx * delta
        self.by += self.vy * delta

        if self.by - BALL_RADIUS <= 0 and self.vy < 0:
            self.by = BALL_RADIUS
            self.vy *= -1
            sounds.append([260, 0.025, "sine"])
        elif self.by + BALL_RADIUS >= PONG_HEIGHT and self.vy > 0:
            self.by = PONG_HEIGHT - BALL_RADIUS
            self.vy *= -1
            sounds.append([260, 0.025, "sine"])

        if (
            self.vx < 0
            and self.bx - BALL_RADIUS <= PADDLE_LEFT_X + PADDLE_WIDTH
            and self.bx > PADDLE_LEFT_X
            and self.by + BALL_RADIUS >= self.py
            and self.by - BALL_RADIUS <= self.py + PADDLE_HEIGHT
        ):
            self._hit_paddle(self.py, 1, sounds)
        elif (
            self.vx > 0
            and self.bx + BALL_RADIUS >= PADDLE_RIGHT_X
            and self.bx < PADDLE_RIGHT_X + PADDLE_WIDTH
            and self.by + BALL_RADIUS >= self.cy
            and self.by - BALL_RADIUS <= self.cy + PADDLE_HEIGHT
        ):
            self._hit_paddle(self.cy, -1, sounds)

        if self.bx < -BALL_RADIUS:
            self._score_point(False, sounds)
        elif self.bx > PONG_WIDTH + BALL_RADIUS:
            self._score_point(True, sounds)

        return True, sounds

    async def broadcast(self, payload: dict, exclude: web.WebSocketResponse | None = None):
        for peer in list(self.clients):
            if peer is exclude or peer.closed:
                continue
            try:
                await peer.send_json(payload)
            except Exception:
                pass

    async def broadcast_lobby(self):
        players = self.count_players()
        await self.broadcast({
            "type": "lobby",
            "players": players,
            "ready": players >= 2,
        })

    async def broadcast_state(self, sounds: list | None = None):
        msg = {"type": "state", "state": self.snapshot()}
        if sounds:
            msg["sound"] = sounds[0]
        await self.broadcast(msg)

    async def run_loop(self):
        try:
            while True:
                await asyncio.sleep(TICK_INTERVAL)
                if not self.is_ready():
                    continue
                changed, sounds = self.step(TICK_INTERVAL)
                if changed or sounds:
                    await self.broadcast_state(sounds)
        except asyncio.CancelledError:
            pass

    def ensure_loop(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run_loop())

    def stop_loop_if_empty(self):
        if not self.clients and self.task and not self.task.done():
            self.task.cancel()
            self.task = None


async def serve_index(request):
    return web.FileResponse("./index.html")


async def serve_pong(request):
    return web.FileResponse("./pong.html")


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    room_code = (request.query.get("room") or "default").strip().lower()[:24] or "default"
    room = rooms.setdefault(room_code, {"clients": set(), "moves": [], "colors": {}})

    used = {room["colors"].get(c) for c in room["clients"]}
    if "w" not in used:
        assigned_color = "w"
    elif "b" not in used:
        assigned_color = "b"
    else:
        assigned_color = "spectator"

    room["clients"].add(ws)
    room["colors"][ws] = assigned_color

    def player_count():
        return sum(1 for c in room["colors"].values() if c in ("w", "b"))

    async def broadcast_others(payload):
        for peer in list(room["clients"]):
            if peer is not ws and not peer.closed:
                await peer.send_json(payload)

    await ws.send_json({
        "type": "welcome",
        "room": room_code,
        "color": assigned_color,
        "moves": room["moves"],
        "players": player_count()
    })

    await broadcast_others({"type": "presence", "players": player_count()})

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                msg_type = data.get("type")

                if msg_type == "move":
                    sender_color = room["colors"].get(ws)
                    if sender_color not in ("w", "b"):
                        continue
                    move_obj = {
                        "from": data.get("from"),
                        "to": data.get("to"),
                        "promo": data.get("promo")
                    }
                    room["moves"].append(move_obj)
                    await broadcast_others({"type": "move", **move_obj})

                elif msg_type == "reset_request":
                    await broadcast_others({"type": "reset_request"})

                elif msg_type == "reset_accept":
                    room["moves"] = []
                    await broadcast_others({"type": "reset_accept"})

                elif msg_type == "reset_decline":
                    await broadcast_others({"type": "reset_decline"})

                elif msg_type == "reset":
                    room["moves"] = []
                    for peer in list(room["clients"]):
                        if not peer.closed:
                            await peer.send_json({"type": "reset"})
    finally:
        room["clients"].discard(ws)
        room["colors"].pop(ws, None)
        if not room["clients"]:
            rooms.pop(room_code, None)
        else:
            await broadcast_others({"type": "presence", "players": player_count()})

    return ws


async def ws_pong_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    room_code = (request.query.get("room") or "1000").strip()[:16] or "1000"
    room = pong_rooms.get(room_code)
    if room is None:
        room = PongRoom(room_code)
        pong_rooms[room_code] = room
    room.ensure_loop()

    used = {room.sides.get(c) for c in room.clients}
    if "left" not in used:
        side = "left"
    elif "right" not in used:
        side = "right"
    else:
        side = "spectator"

    room.clients.add(ws)
    room.sides[ws] = side

    if not room.is_ready():
        room.reset_match_state(keep_scores=False)

    players = room.count_players()
    await ws.send_json({
        "type": "welcome",
        "room": room_code,
        "side": side,
        "players": players,
        "ready": players >= 2,
        "state": room.snapshot(),
    })
    await room.broadcast_lobby()

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                mtype = data.get("type")

                if mtype == "paddle" and side in ("left", "right"):
                    y_val = float(data.get("y", PONG_HEIGHT / 2 - PADDLE_HEIGHT / 2))
                    y_val = max(0.0, min(PONG_HEIGHT - PADDLE_HEIGHT, y_val))
                    if side == "left":
                        room.py = y_val
                    else:
                        room.cy = y_val
                    await room.broadcast(
                        {"type": "paddle", "side": side, "y": y_val},
                        exclude=ws,
                    )

                elif mtype == "serve" and room.is_ready() and side in ("left", "right"):
                    room.serve()
                    await room.broadcast_state()

                elif mtype == "pause" and room.is_ready() and side in ("left", "right"):
                    room.paused = bool(data.get("paused", False))
                    await room.broadcast({"type": "pause", "paused": room.paused})

                elif mtype == "reset" and room.is_ready() and side in ("left", "right"):
                    room.reset_match_state(keep_scores=False)
                    room.serve()
                    await room.broadcast_state()

                elif mtype == "state":
                    if not room.is_ready():
                        continue
                    for peer in list(room.clients):
                        if peer is not ws and not peer.closed:
                            await peer.send_json(data)
    finally:
        room.clients.discard(ws)
        room.sides.pop(ws, None)

        if not room.is_ready():
            room.reset_match_state(keep_scores=False)
            await room.broadcast_state()

        if not room.clients:
            room.stop_loop_if_empty()
            pong_rooms.pop(room_code, None)
        else:
            await room.broadcast_lobby()

    return ws


app = web.Application()
app.router.add_get("/", serve_index)
app.router.add_get("/ws", ws_handler)
app.router.add_get("/pong", serve_pong)
app.router.add_get("/ws-pong", ws_pong_handler)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Chess.mo Online server running at http://0.0.0.0:{port}")
    web.run_app(app, host="0.0.0.0", port=port)