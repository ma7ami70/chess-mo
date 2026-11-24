import json
import os
from aiohttp import web

rooms = {}  # room_code -> {"clients": set(), "moves": [], "colors": {}}


async def serve_index(request):
    return web.FileResponse("./index.html")


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


app = web.Application()
app.router.add_get("/", serve_index)
app.router.add_get("/ws", ws_handler)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Chess.mo Online server running at http://0.0.0.0:{port}")
    web.run_app(app, host="0.0.0.0", port=port)