#!/usr/bin/env python3
import json, secrets, asyncio, time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn, os, zenoh

# --- open zenoh session ---
z = zenoh.open(zenoh.Config())

# --- caps cache (room -> caps dict) and live subscriber ---
caps_cache = {}

def _caps_handler(sample):
    try:
        key = str(sample.key_expr)      # e.g. webrtc/cam1/caps
        parts = key.split("/")
        room = parts[1] if len(parts) >= 3 else "cam1"
        payload = sample.payload.to_bytes()  # ✅ convert from ZBytes
        caps_cache[room] = json.loads(payload.decode())
        print(f"[bridge] cached caps for room={room}: "
              f"{len(caps_cache[room].get('resolutions', []))} resolutions")
    except Exception as e:
        print("[bridge] caps_handler error:", e)

z.declare_subscriber("webrtc/**/caps", _caps_handler)

app = FastAPI(title="WS↔zenoh bridge")

# --- WebSocket endpoint (define BEFORE mounting static!) ---
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    def ws_send_json(obj):
        asyncio.run_coroutine_threadsafe(ws.send_json(obj), loop)

    client_id = secrets.token_hex(6)
    await ws.send_json({"action":"welcome","client_id":client_id})
    subs = []
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action"); room = msg.get("room","cam1")

            if action == "subscribe":
                print(f"[bridge] subscribe from WS for room={room}")
                room_caps = caps_cache.get(room)
                if room_caps is None:
                    print(f"[bridge] no caps cached, sending caps_req to webrtc/{room}/caps_req")
                    z.put(f"webrtc/{room}/caps_req", b"1")
                    for _ in range(20):   # wait up to 2s
                        await asyncio.sleep(0.1)
                        room_caps = caps_cache.get(room)
                        if room_caps is not None:
                            print(f"[bridge] caps arrived after request for room={room}")
                            break
                await ws.send_json({"action":"subscribed","room":room,"caps": room_caps})

                # then set up the per-viewer answer/ice subscribers (unchanged)
                subs.append(z.declare_subscriber(
                    f"webrtc/{room}/viewers/{client_id}/answer",
                    lambda s: ws_send_json({
                        "action":"answer","type":"answer",
                        "sdp": json.loads(s.payload.to_bytes().decode())["sdp"]})
                ))

                subs.append(z.declare_subscriber(
                    f"webrtc/{room}/viewers/{client_id}/ice",
                    lambda s: ws_send_json({
                        "action":"ice",
                        "candidate": json.loads(s.payload.to_bytes().decode())["candidate"]})
                ))

            elif action == "offer":
                z.put(
                    f"webrtc/{room}/viewers/{client_id}/offer",
                    json.dumps({"sdp": msg["sdp"], "constraints": msg.get("constraints", {})}).encode(),
                )
                ## Log to see activity ##
                print(f"[bridge] →pub offer room={room} id={client_id}")

            elif action == "ice":
                cand = msg.get("candidate")
                if cand:
                    z.put(
                        f"webrtc/{room}/viewers/{client_id}/ice",
                        json.dumps({"candidate": cand}).encode(),
                    )
                    ## Log to see activity ##
                    print(f"[bridge] →pub ice   room={room} id={client_id}")

    except WebSocketDisconnect:
        pass
    finally:
        for s in subs:
            try: s.undeclare()
            except Exception: pass

# --- static hosting (after /ws is defined) ---
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# if you placed viewer.html at repo root, copy or symlink it into static as index.html
# cp -f ~/TTT/WebRTC_Humble/viewer.html ~/TTT/WebRTC_Humble/ws_zenoh_bridge/static/index.html

@app.get("/")
async def root_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h3>Bridge up. Put viewer at ws_zenoh_bridge/static/index.html</h3>")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    # listen on 9296 as you typed in the page
    uvicorn.run(app, host="0.0.0.0", port=9296)