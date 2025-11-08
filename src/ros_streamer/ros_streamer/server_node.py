#!/usr/bin/env python3
import asyncio, threading, time, json, re, subprocess
from pathlib import Path

import rclpy
from rclpy.node import Node

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse

from ament_index_python.packages import get_package_share_directory

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRelay
import av

import zenoh
from aiortc import RTCIceCandidate
from aiortc.sdp import candidate_from_sdp

def _to_rtc_candidate(cdict):
    if cdict is None:
        return None
    if isinstance(cdict, RTCIceCandidate):
        return cdict

    cand_line = cdict.get("candidate")
    if not cand_line:
        return None

    base = candidate_from_sdp(cand_line)

    sdp_mid = cdict.get("sdpMid")
    try:
        sdp_mline = int(cdict.get("sdpMLineIndex", 0))
    except Exception:
        sdp_mline = 0

    if isinstance(base, RTCIceCandidate):
        try:
            base.sdpMid = sdp_mid
            base.sdpMLineIndex = sdp_mline
        except Exception:
            pass
        return base

    kwargs = dict(base)
    if "relatedAddress" not in kwargs and "relAddr" in kwargs:
        kwargs["relatedAddress"] = kwargs.pop("relAddr")
    if "relatedPort" not in kwargs and "relPort" in kwargs:
        kwargs["relatedPort"] = kwargs.pop("relPort")
    kwargs["sdpMid"] = sdp_mid
    kwargs["sdpMLineIndex"] = sdp_mline
    return RTCIceCandidate(**kwargs)

# ---------- per-viewer resizer ----------
class ResizedTrack(MediaStreamTrack):
    kind = "video"
    def __init__(self, source_track: MediaStreamTrack, width: int, height: int, fps: int | None):
        super().__init__()
        import math
        self._source = source_track
        self._w = max(160, int(width)); self._h = max(120, int(height))
        self._fps = int(fps) if fps else None
        self._min_interval = (1.0 / self._fps) if self._fps and self._fps > 0 else 0.0
        self._next_ts = 0.0
    async def recv(self) -> av.VideoFrame:
        while True:
            frame: av.VideoFrame = await self._source.recv()
            if self._min_interval > 0.0:
                now = time.time()
                if now < self._next_ts: continue
                self._next_ts = now + self._min_interval
            if frame.width != self._w or frame.height != self._h:
                frame = frame.reformat(width=self._w, height=self._h)
            return frame

class RosStreamerNode(Node):
    def __init__(self):
        super().__init__('ros_streamer_server')

        def _parse_stuns(raw):
            """Return [] if empty/'none'/'-', or list of STUN URLs otherwise."""
            if isinstance(raw, str):
                v = raw.strip()
                if v.lower() in ('', 'none', '-'):
                    return []
                return [s.strip() for s in v.split(',') if s.strip()]
            if isinstance(raw, (list, tuple)):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            return []
        
        # ---- params ----
        self.declare_parameter('device', '/dev/video0')
        self.declare_parameter('width', 1280)
        self.declare_parameter('height', 720)
        self.declare_parameter('fps', 30)
        self.declare_parameter('host', '0.0.0.0')
        self.declare_parameter('port', 8000)
        self.declare_parameter('stun_urls', '')  # CSV or ""
        self.declare_parameter('room', 'cam1')
        self.declare_parameter('zenoh_enable', True)
        self.declare_parameter('zenoh_config', '')  # optional file path or JSON string

        self._zenoh = None
        self.device = self.get_parameter('device').get_parameter_value().string_value or '/dev/video0'
        self._players = {}          # viewer_id -> MediaPlayer (keep strong refs)
        self._pending_ice = {}      # viewer_id -> [ice dict ...]
        self._viewer_pcs = {}       # viewer_id -> RTCPeerConnection
        self._relay = MediaRelay()
        self._cam_player = None 
        self._viewer_refs = {}
        self.width  = int(self.get_parameter('width').value)
        self.height = int(self.get_parameter('height').value)
        self.fps    = int(self.get_parameter('fps').value)
        self.host   = self.get_parameter('host').value
        self.port   = int(self.get_parameter('port').value)
        raw_stun    = self.get_parameter('stun_urls').value
        self.stun_urls = _parse_stuns(raw_stun)
        self.room   = self.get_parameter('room').value
        self.zenoh_enable = bool(self.get_parameter('zenoh_enable').value)
        self.zenoh_config = self.get_parameter('zenoh_config').value

        # Probe camera capabilities (best-effort)
        self._caps = []
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "--list-formats-ext", "-d", self.device],
                stderr=subprocess.STDOUT, text=True
            )
            self._caps = self._parse_v4l2_caps(out)
            self.get_logger().info(f"Found {len(self._caps)} camera modes on {self.device}")
            if not self._caps:
                self.get_logger().warn("v4l2-ctl parsed 0 modes; enabling fallback modes.")
        except Exception as e:
            self.get_logger().warn(f"Could not probe caps for {self.device}: {e}\nEnabling fallback modes.")

        ## Optional plan if cannot get caps ##
        # if not self._caps:
        #     # Common C920/C920e MJPG modes — adjust if your camera differs
        #     fallback_res = [
        #         (1920,1080,[30]),
        #         (1280,720,[30,60]),
        #         (960,540,[30]),
        #         (848,480,[30]),
        #         (800,600,[30]),
        #         (640,480,[30,60]),
        #         (424,240,[30]),
        #         (320,240,[30])
        #     ]
        #     self._caps = [{"pixfmt":"MJPG","width":w,"height":h,"fps":fps} for (w,h,fps) in fallback_res]
        
        # ---- static (serve test UI) ----
        candidates = []
        try:
            share_dir = Path(get_package_share_directory('ros_streamer'))
            candidates.append(share_dir / 'static')
        except Exception:
            pass
        candidates += [Path(__file__).parent/'static', Path.cwd()/'ros_streamer'/'static']
        static_dir = next((p for p in candidates if p.exists()), None)
        if static_dir is None:
            raise RuntimeError("Static directory not found; rebuild with --symlink-install")
        self.app = FastAPI(title='ROS Streamer')

        self._loop = None  # event loop used by uvicorn / FastAPI

        async def _capture_loop():
            # runs inside uvicorn's asyncio loop
            self._loop = asyncio.get_running_loop()

        self.app.add_event_handler("startup", _capture_loop)

        self.app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')
        
        @self.app.get('/')
        async def index(): return FileResponse(str(static_dir / 'index.html'))
        
        @self.app.get('/caps')
        async def caps():
            return JSONResponse(self.build_caps_payload())

        # ---- open camera once + relay ----
        def _make_player():
            opts = {'video_size': f'{self.width}x{self.height}', 'framerate': f'{self.fps}'}
            return MediaPlayer(self.device, format='v4l2', options=opts)
        self._player = _make_player()

        # ---- legacy HTTP /offer (still available) ----
        self.pcs = set()
        @self.app.post('/offer')
        async def offer(request: Request):
            params = await request.json()
            offer = RTCSessionDescription(sdp=params['sdp'], type=params['type'])
            cons = params.get('constraints', {}) or {}
            cw = int(cons.get('width',  self.width)); ch = int(cons.get('height', self.height))
            cfps = int(cons.get('fps', 0)) or None
            cfg = {} if not self.stun_urls else {"iceServers": [{"urls": self.stun_urls}]}
            pc = RTCPeerConnection(configuration=cfg); self.pcs.add(pc)
            @pc.on('connectionstatechange')
            async def on_state_change():
                st = pc.connectionState
                self.get_logger().info(f'HTTP peer state: {st}')
                if st in ('failed','closed','disconnected'):
                    await pc.close()
                    self.pcs.discard(pc)
            base = self._relay.subscribe(self._player.video)
            pc.addTrack(ResizedTrack(base, cw, ch, cfps))
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await self._wait_ice_gathering_complete(pc)
            return JSONResponse({'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type})

        # ---- run HTTP in background ----
        import uvicorn
        self._uvicorn_server = uvicorn.Server(uvicorn.Config(self.app, host=self.host, port=self.port, log_level='info'))
        threading.Thread(target=self._run_http, daemon=True).start()
        self.get_logger().info(f'HTTP test UI at http://{self.host}:{self.port}/')

        # ---- zenoh signaling ----
        if bool(self.zenoh_enable):
            threading.Thread(target=self._run_zenoh, daemon=True).start()
            self.get_logger().info('Zenoh signaling is enabled.')

    def _open_player(self, constraints: dict) -> MediaPlayer:
        """Open /dev/video* honoring width/height/fps; prefer MJPEG then fallback to YUYV."""
        width  = int(constraints.get("width")  or 640)
        height = int(constraints.get("height") or 480)
        fps    = int(constraints.get("fps")    or 30)
        opts = {"video_size": f"{width}x{height}", "framerate": str(fps)}

        # Try MJPEG first (Logitech C920e friendly), then fallback to YUYV.
        try:
            return MediaPlayer(self.device, format="v4l2", options={**opts, "input_format": "mjpeg"})
        except Exception:
            return MediaPlayer(self.device, format="v4l2", options={**opts, "input_format": "yuyv422"})

    def _get_shared_player(self, constraints: dict) -> MediaPlayer:
        """
        Return the singleton MediaPlayer; if already opened with different caps,
        reuse it (log a warning) so we avoid reopening the device.
        """
        if self._cam_player is None:
            try:
                self._cam_player = self._open_player(constraints)
                self.get_logger().info(f"Camera opened on {self.device}")
            except OSError as e:
                # If busy, just reuse the existing player (if any), else re-raise.
                if "Device or resource busy" in str(e) and self._cam_player is not None:
                    self.get_logger().warn("Device busy; reusing existing camera player")
                else:
                    raise
        return self._cam_player

    def _schedule(self, coro):
        """
        Run a coroutine on the app's asyncio loop, even if we're in a non-async thread (zenoh callbacks).
        """
        loop = self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            # Fallback (shouldn't usually happen after startup):
            asyncio.run(coro)

    def build_player(self, constraints: dict):
        """
        Create a v4l2 MediaPlayer for /dev/video* honoring width/height/fps.
        Tries MJPEG first (Logitech prefers this), then falls back to YUYV.
        constraints example: {"width": 640, "height": 480, "fps": 30}
        """
        # Defaults if UI didn’t send values
        width  = int(constraints.get("width")  or 640)
        height = int(constraints.get("height") or 480)
        fps    = int(constraints.get("fps")    or 30)

        base_opts = {
            "video_size": f"{width}x{height}",
            "framerate":  str(fps),
        }

        # Prefer MJPEG for C920e; fall back to YUYV if the camera mode isn't supported
        try:
            return MediaPlayer(self.device, format="v4l2",
                            options={**base_opts, "input_format": "mjpeg"})
        except Exception:
            # Fallback works with the YUYV modes in your v4l2-ctl dump
            return MediaPlayer(self.device, format="v4l2",
                            options={**base_opts, "input_format": "yuyv422"})

    # ---------- threads ----------
    def _run_http(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().run_until_complete(self._uvicorn_server.serve())

    async def _wait_ice_gathering_complete(self, pc):
        if pc.iceGatheringState == "complete":
            return
        fut = asyncio.get_event_loop().create_future()
        def check():
            if pc.iceGatheringState == "complete" and not fut.done():
                fut.set_result(True)
        pc.on("icegatheringstatechange", check)  # aiortc attaches callbacks like this too
        while not fut.done():
            await asyncio.sleep(0.02)

    def _run_zenoh(self):
        try:
            cfg = zenoh.Config()
            if isinstance(self.zenoh_config, str) and self.zenoh_config.strip():
                try:
                    cfg = zenoh.Config.from_file(self.zenoh_config)
                except Exception:
                    cfg = zenoh.Config.from_json(self.zenoh_config)
            self._zenoh = zenoh.open(cfg)
            self.get_logger().info('Zenoh session opened.')
        except Exception as e:
            self.get_logger().error(f'Zenoh open failed: {e}')
            return

        room = self.room        
        payload = self.build_caps_payload()
        self.get_logger().info(f"Publishing caps to webrtc/{room}/caps with {len(payload['resolutions'])} resolutions")
        self._zenoh.put(f"webrtc/{room}/caps", json.dumps(payload).encode())

        def on_offer(sample):
            """
            Viewer -> Publisher OFFER handler (zenoh subscriber callback).
            Key: webrtc/<room>/viewers/<viewer_id>/offer
            Payload: {"sdp": "...","constraints":{"width":W,"height":H,"fps":F}}
            """
            try:
                key = str(sample.key_expr)  # e.g. "webrtc/cam1/viewers/<id>/offer"
                parts = key.split("/")
                if len(parts) < 5:
                    self.get_logger().warn(f"Offer key malformed: {key}")
                    return
                viewer_id = parts[-2]
                payload = sample.payload.to_bytes().decode()
                msg = json.loads(payload)
                offer_sdp = msg.get("sdp")
                constraints = msg.get("constraints", {}) or {}
                if not offer_sdp:
                    self.get_logger().warn(f"Offer missing SDP for {viewer_id}")
                    return
            except Exception as e:
                self.get_logger().warn(f"Bad offer sample: {e}")
                return

            async def _handle():
                # 1) PC: get or create
                pc = self._viewer_pcs.get(viewer_id)
                if pc is None:
                    from aiortc import RTCPeerConnection, MediaStreamTrack
                    pc = RTCPeerConnection()
                    self._viewer_pcs[viewer_id] = pc
                    self.get_logger().info(f"PC created for {viewer_id}")

                    # 1a) send server ICE to viewer
                    @pc.on("icecandidate")
                    async def __on_ice(ev):
                        if not ev.candidate:
                            return
                        try:
                            cand = {
                                "candidate": ev.candidate.candidate,
                                "sdpMid": ev.candidate.sdpMid,
                                "sdpMLineIndex": ev.candidate.sdpMLineIndex,
                            }
                            self._zenoh.put(
                                f"webrtc/{room}/viewers/{viewer_id}/ice",
                                json.dumps({"candidate": cand}).encode(),
                            )
                            # Optional: log address from SDP line
                            self.get_logger().info(f"pub ICE -> {viewer_id}: {cand}")
                        except Exception as e:
                            self.get_logger().warn(f"pub ICE send failed: {e}")

                # 2) Attach camera track according to constraints (width/height/fps)
                try:
                    cam = self._get_shared_player(constraints)           # one camera for all
                    pc.addTransceiver("video", direction="sendonly")     # ensure sendonly m-line
                    if getattr(cam, "video", None):
                        relay_track = self._relay.subscribe(cam.video)   # clone frames for this viewer
                        pc.addTrack(relay_track)
                        self._viewer_refs[viewer_id] = relay_track       # keep a strong ref
                    self.get_logger().info(f"Track added for {viewer_id} with {constraints}")
                except Exception as e:
                    self.get_logger().warn(f"Attach track failed for {viewer_id}: {e}")

                try:
                    # 3) Apply remote offer
                    from aiortc import RTCSessionDescription
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
                    self.get_logger().info(f"Offer applied for {viewer_id}")

                    # 4) Drain any ICE that arrived before the offer
                    for cand in self._pending_ice.pop(viewer_id, []):
                        try:
                            await pc.addIceCandidate(_to_rtc_candidate(cand))
                        except Exception as e:
                            self.get_logger().warn(f"addIceCandidate failed (drain) for {viewer_id}: {e}")

                    # 5) Create & set local answer
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    # 6) Wait for ICE gather complete so the answer SDP is useful on LAN
                    await self._wait_ice_gathering_complete(pc)
                    # 7) Publish answer
                    self._zenoh.put(
                        f"webrtc/{room}/viewers/{viewer_id}/answer",
                        json.dumps({"sdp": pc.localDescription.sdp}).encode(),
                    )
                    self.get_logger().info(f"Answer published for {viewer_id}")
                except Exception as e:
                    self.get_logger().warn(f"Offer handling failed for {viewer_id}: {e}")

            # schedule the async handling
            # asyncio.create_task(_handle())
            self._schedule(_handle())

        def on_ice(sample):
            """
            Viewer -> Publisher ICE handler (zenoh subscriber callback).
            Key: webrtc/<room>/viewers/<viewer_id>/ice
            """
            try:
                key = str(sample.key_expr)  # e.g. "webrtc/cam1/viewers/<id>/ice"
                parts = key.split("/")
                if len(parts) < 5:
                    self.get_logger().warn(f"ICE key malformed: {key}")
                    return
                viewer_id = parts[-2]

                payload = sample.payload.to_bytes().decode()
                msg = json.loads(payload)
                cand_dict = msg.get("candidate")
                if not cand_dict:
                    self.get_logger().warn(f"ICE sample missing 'candidate' for {viewer_id}")
                    return
            except Exception as e:
                self.get_logger().warn(f"Bad ICE sample: {e}")
                return

            pc = self._viewer_pcs.get(viewer_id)

            # If PC not ready, buffer raw dicts (we'll parse on drain)
            if pc is None or pc.remoteDescription is None:
                self._pending_ice.setdefault(viewer_id, []).append(cand_dict)
                self.get_logger().info(f"Buffered ICE for {viewer_id} (pending={len(self._pending_ice[viewer_id])})")
                return

            # Otherwise parse -> RTCIceCandidate and add
            async def _add():
                try:
                    await pc.addIceCandidate(_to_rtc_candidate(cand_dict))
                except Exception as e:
                    self.get_logger().warn(f"addIceCandidate failed (live) for {viewer_id}: {e}")
            # asyncio.create_task(_add())
            self._schedule(_add())

        def on_caps_req(sample):
            self.get_logger().info(f"caps_req received on webrtc/{room}/caps_req, re-publishing caps")
            self._zenoh.put(f"webrtc/{room}/caps", json.dumps(self.build_caps_payload()).encode())


        sub_caps_req = self._zenoh.declare_subscriber(
            f"webrtc/{room}/caps_req", on_caps_req
        )

        sub_offer = self._zenoh.declare_subscriber(
            f"webrtc/{room}/viewers/*/offer", on_offer
        )
        sub_ice = self._zenoh.declare_subscriber(
            f"webrtc/{room}/viewers/*/ice", on_ice
        )

        try:
            while rclpy.ok(): time.sleep(1.0)
        finally:
            sub_offer.undeclare()
            sub_ice.undeclare()
            sub_caps_req.undeclare()
            if self._zenoh: self._zenoh.close()

    async def _handle_offer(self, viewer_id: str, msg: dict):
        cons = msg.get("constraints") or {}
        cw = int(cons.get('width',  self.width)); ch = int(cons.get('height', self.height))
        cfps = int(cons.get('fps', 0)) or None

        old = self._viewer_pcs.pop(viewer_id, None)
        if old:
            try: await old.close()
            except Exception: pass

        cfg = {} if not self.stun_urls else {"iceServers": [{"urls": self.stun_urls}]}
        pc = RTCPeerConnection(configuration=cfg)
        self._viewer_pcs[viewer_id] = pc

        room = self.room; z = self._zenoh

        @pc.on("icecandidate")
        async def _on_ice(ev):
            if not ev.candidate:
                return
            try:
                z.put(
                    f"webrtc/{room}/viewers/{viewer_id}/ice",
                    json.dumps({
                        "candidate": {
                            "candidate": ev.candidate.candidate,           # SDP line
                            "sdpMid": ev.candidate.sdpMid,
                            "sdpMLineIndex": ev.candidate.sdpMLineIndex,
                        }
                    }).encode(),
                )
            except Exception as e:
                self.get_logger().warn(f"pub ICE send failed: {e}")

        @pc.on("connectionstatechange")
        async def _on_state():
            st = pc.connectionState
            self.get_logger().info(f"viewer {viewer_id} state: {st}")
            if st in ("failed","closed","disconnected"):
                await pc.close()
                self._viewer_pcs.pop(viewer_id, None)
                await self._release_viewer(viewer_id)

        base = self._relay.subscribe(self._player.video)
        pc.addTrack(ResizedTrack(base, width=cw, height=ch, fps=cfps))
        
        await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))

        # after: await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))
        for cand in self._pending_ice.pop(viewer_id, []):
            try:
                await pc.addIceCandidate(_to_rtc_candidate(cand))
            except Exception as e:
                self.get_logger().warn(f"addIceCandidate failed (drain) for {viewer_id}: {e}")

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await self._wait_ice_gathering_complete(pc)  # keep this
        self._zenoh.put(
            f"webrtc/{room}/viewers/{viewer_id}/answer",
            json.dumps({"sdp": pc.localDescription.sdp}).encode()
        )

    async def _close_http_pcs(self):
        await asyncio.gather(*[pc.close() for pc in list(self.pcs)], return_exceptions=True)
        self.pcs.clear()
    
    async def _close_viewer_pcs(self):
        async def _release_viewer(self, viewer_id: str):
            # Drop the relay subscription for this viewer
            self._viewer_refs.pop(viewer_id, None)

            # If nobody is viewing anymore, stop the shared camera
            if not self._viewer_refs and self._cam_player is not None:
                try:
                    if hasattr(self._cam_player, "stop"):
                        await self._cam_player.stop()
                    self.get_logger().info("Camera player stopped")
                except Exception as e:
                    self.get_logger().warn(f"Failed to stop camera player: {e}")
                self._cam_player = None

        await asyncio.gather(*[pc.close() for pc in list(self._viewer_pcs.values())],
                            return_exceptions=True)
        self._viewer_pcs.clear()
        # stop and release per-viewer players
        for vid, player in list(self._players.items()):
            try:
                if hasattr(player, "stop"):
                    await player.stop()
            except Exception as e:
                self.get_logger().warn(f"Failed to stop player for {vid}: {e}")
            self._players.pop(vid, None)

        for vid in list(self._viewer_refs.keys()):
            await self._release_viewer(vid)


    def destroy_node(self):
        try:
            asyncio.run(self._close_http_pcs())
        except RuntimeError:
            pass
        try:
            asyncio.run(self._close_viewer_pcs())
        except RuntimeError:
            pass
        try:
            if self._player:
                self._player.audio and self._player.audio.stop()
                self._player.video and self._player.video.stop()
        except Exception:
            pass
        return super().destroy_node()
    
    def _parse_v4l2_caps(self, text: str):
        modes = []
        fmt = None
        w = h = None

        # Normalize lines (strip & collapse spaces)
        lines = [ln.strip() for ln in text.splitlines()]

        # Regexes for different formats of the same info
        re_fmt    = re.compile(r"Pixel\s*Format:\s*'([^']+)'|^\[\d+\]\s*:\s*'([^']+)'")
        re_size1  = re.compile(r"(?:Size|Frame size)\s*:\s*Discrete\s*(\d+)\s*x\s*(\d+)", re.I)
        re_size2  = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$")  # rare variant
        re_intv1  = re.compile(r"(?:Interval|Frame interval)\s*:\s*Discrete\s*([\d\.]+)\s*fps", re.I)
        re_intv2  = re.compile(r"\(([\d\.]+)\s*fps\)", re.I)

        def _add_mode(ff, width, height, fps):
            existing = next((m for m in modes if m["pixfmt"]==ff and m["width"]==width and m["height"]==height), None)
            if not existing:
                existing = {"pixfmt": ff, "width": width, "height": height, "fps": []}
                modes.append(existing)
            if fps not in existing["fps"]:
                existing["fps"].append(fps)

        for ln in lines:
            m = re_fmt.search(ln)
            if m:
                fmt = m.group(1) or m.group(2)
                w = h = None
                continue

            m = re_size1.search(ln) or re_size2.search(ln)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                continue

            if fmt and w and h:
                m = re_intv1.search(ln) or re_intv2.search(ln)
                if m:
                    try:
                        fps_val = int(round(float(m.group(1))))
                        if fps_val > 0:
                            _add_mode(fmt, w, h, fps_val)
                    except Exception:
                        pass

        # Sort for nice UI
        for m in modes:
            m["fps"].sort()
        modes.sort(key=lambda k: (k["width"]*k["height"], k["width"], k["height"], k["pixfmt"]))
        return modes    

    def build_caps_payload(self):
        by_res = {}
        for m in self._caps:
            key = f'{m["width"]}x{m["height"]}'
            by_res.setdefault(key, set())
            for f in m["fps"]:
                by_res[key].add(f)
        # convert to lists
        by_res = {k: sorted(list(v)) for k, v in by_res.items()}
        resolutions = sorted(by_res.keys(),
                            key=lambda s: (int(s.split('x')[0]) * int(s.split('x')[1]), s))
        return {"device": self.device, "resolutions": resolutions, "fps_by_resolution": by_res}

    

def main(args=None):
    rclpy.init(args=args)
    node = RosStreamerNode()
    try: rclpy.spin(node)
    except KeyboardInterrupt: pass
    finally:
        node.get_logger().info('Shutting down ROS Streamer...')
        node.destroy_node()
        rclpy.shutdown()
