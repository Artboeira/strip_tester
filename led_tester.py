#!/usr/bin/env python3
"""
LED Strip Tester — Art-Net / DMX controller
Requires: aiohttp  (pip install aiohttp)
Run:      python led_tester.py [--port 8080] [--no-browser]
"""

import argparse
import asyncio
import colorsys
import json
import math
import os
import random
import socket
import struct
import time
import webbrowser

from aiohttp import web


# ──────────────────────────────────────────────────────────────────────────────
# Art-Net Sender
# ──────────────────────────────────────────────────────────────────────────────

class ArtNetSender:
    """Sends ArtDMX packets (Art-Net protocol) over UDP."""
    PORT = 6454

    def __init__(self, ip: str):
        self.ip = ip
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send_universe(self, universe: int, data: bytes) -> None:
        if len(data) % 2:
            data += b'\x00'
        packet = (
            b'Art-Net\x00'           # ID (8 bytes)
            b'\x00\x50'              # OpCode: ArtDMX (0x5000 LE)
            b'\x00\x0e'              # ProtVer: 14 (BE)
            + bytes([0, 0])          # Sequence=0, Physical=0
            + struct.pack('<H', universe & 0x7FFF)  # Universe (LE, 15-bit)
            + struct.pack('>H', len(data))           # Length (BE)
            + data
        )
        try:
            self._sock.sendto(packet, (self.ip, self.PORT))
        except Exception:
            pass

    def close(self) -> None:
        self._sock.close()


# ──────────────────────────────────────────────────────────────────────────────
# Strip Manager  —  config + DMX mapping + send
# ──────────────────────────────────────────────────────────────────────────────

class StripManager:
    def __init__(self):
        self.artnet_ip: str = '2.0.0.1'
        self.strips: list[dict] = []        # [{'name': str, 'pixels': int, 'universe_offset': int, 'start_channel': int}]
        self._sender: ArtNetSender | None = None

    # ── internal ──

    def _sender_for(self) -> ArtNetSender:
        if self._sender is None:
            self._sender = ArtNetSender(self.artnet_ip)
        return self._sender

    # ── public API ──

    def update_config(self, ip: str, strips: list[dict]) -> None:
        if ip != self.artnet_ip:
            if self._sender:
                self._sender.close()
                self._sender = None
            self.artnet_ip = ip
        self.strips = strips

    def get_universe_map(self) -> list[dict]:
        """
        Returns per-strip segments describing which Art-Net universe and
        channel range (1-indexed) each strip occupies.
        Each strip starts at universe_offset * 512 + (start_channel - 1).
        A pixel can straddle a universe boundary — the map reflects raw channels.
        """
        result = []
        for strip in self.strips:
            n = strip['pixels']
            if n == 0:
                result.append({'name': strip['name'], 'pixels': 0, 'segments': []})
                continue
            u_off  = strip.get('universe_offset', 0)
            ch1    = strip.get('start_channel', 1)          # 1-indexed
            global_start = u_off * 512 + (ch1 - 1)
            global_end   = global_start + n * 3 - 1
            segments = []
            for u in range(global_start // 512, global_end // 512 + 1):
                seg_s = max(global_start, u * 512)
                seg_e = min(global_end, (u + 1) * 512 - 1)
                segments.append({
                    'universe': u,
                    'ch_start': seg_s % 512 + 1,    # 1-indexed display
                    'ch_end':   seg_e % 512 + 1,
                    'channels': seg_e - seg_s + 1,
                })
            result.append({'name': strip['name'], 'pixels': n, 'segments': segments})
        return result

    def send_all(self, all_pixels: list[list[tuple]]) -> dict[int, list[int]]:
        """
        Write each strip's RGB data into sparse per-universe byte buffers
        respecting each strip's universe_offset and start_channel.
        Returns {universe: [ch_values]} for the DMX monitor.
        """
        universe_bufs: dict[int, bytearray] = {}

        for i, strip in enumerate(self.strips):
            pix = all_pixels[i] if i < len(all_pixels) else []
            if not pix:
                continue
            u_off  = strip.get('universe_offset', 0)
            ch1    = strip.get('start_channel', 1)
            global_pos = u_off * 512 + (ch1 - 1)
            for r, g, b in pix:
                for val in (r, g, b):
                    u  = global_pos // 512
                    ch = global_pos % 512
                    if u not in universe_bufs:
                        universe_bufs[u] = bytearray(512)
                    universe_bufs[u][ch] = val
                    global_pos += 1

        if not universe_bufs:
            return {}

        sender = self._sender_for()
        universe_data: dict[int, list[int]] = {}
        for u in sorted(universe_bufs.keys()):
            data = bytes(universe_bufs[u])
            universe_data[u] = list(data)
            sender.send_universe(u, data)   # always even (512 bytes)

        return universe_data


# ──────────────────────────────────────────────────────────────────────────────
# Animation Engine  —  async loop computing pixel values per effect
# ──────────────────────────────────────────────────────────────────────────────

class AnimationEngine:
    FPS = 40

    def __init__(self, sm: StripManager):
        self.sm = sm
        self.effect:     str   = 'rainbow_wave'
        self.color:      tuple = (255, 0, 0)
        self.brightness: float = 1.0
        self.speed:      float = 0.5

        self._all_pixels:    list  = []
        self._universe_data: dict  = {}
        self._fire_heat:     dict  = {}   # {strip_idx: [heat values]}
        self._twinkle:       dict  = {}   # {strip_idx: [float values]}
        self._task: asyncio.Task | None = None

    # ── helpers ──

    def _dim(self, r: int, g: int, b: int) -> tuple:
        br = self.brightness
        return (int(r * br), int(g * br), int(b * br))

    def _hsv_pixel(self, h: float) -> tuple:
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)
        return self._dim(int(r * 255), int(g * 255), int(b * 255))

    # ── per-strip effect ──

    def _effect_pixels(self, idx: int, n: int, t: float) -> list[tuple]:
        if n == 0:
            return []

        sp = 0.2 + self.speed * 3.0
        r, g, b = self.color

        if self.effect == 'solid':
            return [self._dim(r, g, b)] * n

        if self.effect == 'breathing':
            f  = (math.sin(t * sp * math.pi) + 1) * 0.5
            br = self.brightness * f
            return [(int(r * br), int(g * br), int(b * br))] * n

        if self.effect == 'rainbow_solid':
            return [self._hsv_pixel(i / n) for i in range(n)]

        if self.effect == 'rainbow_wave':
            off = (t * sp * 0.25) % 1.0
            return [self._hsv_pixel(i / n + off) for i in range(n)]

        if self.effect == 'chase':
            pos = int(t * sp * 20) % n
            px = [(0, 0, 0)] * n
            tail = max(3, n // 20)
            for j in range(tail):
                fade = (tail - j) / tail
                pr, pg, pb = r, g, b
                px[(pos - j) % n] = (
                    int(pr * fade * self.brightness),
                    int(pg * fade * self.brightness),
                    int(pb * fade * self.brightness),
                )
            return px

        if self.effect == 'theater_chase':
            pos = int(t * sp * 4) % 3
            return [self._dim(r, g, b) if i % 3 == pos else (0, 0, 0) for i in range(n)]

        if self.effect == 'fire':
            heat = self._fire_heat.get(idx)
            if heat is None or len(heat) != n:
                heat = [0] * n
            # Cool
            cool_max = max(2, 55 * 10 // n + 2)
            for i in range(n):
                heat[i] = max(0, heat[i] - random.randint(0, cool_max))
            # Drift upward
            for i in range(n - 1, 1, -1):
                heat[i] = (heat[i - 1] + heat[i - 2] * 2) // 3
            # Spark at base
            if random.random() < 0.55:
                y = random.randint(0, min(7, n - 1))
                heat[y] = min(255, heat[y] + random.randint(160, 255))
            self._fire_heat[idx] = heat
            pixels = []
            for h in heat:
                f = h / 255.0
                if f < 0.4:
                    pixels.append(self._dim(int(f / 0.4 * 255), 0, 0))
                elif f < 0.7:
                    pixels.append(self._dim(255, int((f - 0.4) / 0.3 * 255), 0))
                else:
                    pixels.append(self._dim(255, 255, int((f - 0.7) / 0.3 * 255)))
            return pixels

        if self.effect == 'twinkle':
            tw = self._twinkle.get(idx)
            if tw is None or len(tw) != n:
                tw = [random.random() for _ in range(n)]
            for i in range(n):
                tw[i] = max(0.0, min(1.0, tw[i] + random.uniform(-0.08, 0.10) * sp))
            self._twinkle[idx] = tw
            return [
                (int(r * v * self.brightness), int(g * v * self.brightness), int(b * v * self.brightness))
                for v in tw
            ]

        if self.effect == 'strobe':
            on = int(t * sp * 6) % 2 == 0
            return [self._dim(r, g, b) if on else (0, 0, 0)] * n

        if self.effect == 'color_wipe':
            period = n * 2
            pos = int((t * sp * 15) % period)
            if pos < n:
                return [self._dim(r, g, b) if i <= pos else (0, 0, 0) for i in range(n)]
            else:
                p2 = pos - n
                return [(0, 0, 0) if i <= p2 else self._dim(r, g, b) for i in range(n)]

        return [(0, 0, 0)] * n

    # ── async loop ──

    async def _run(self) -> None:
        frame = 1.0 / self.FPS
        while True:
            t0 = time.monotonic()
            t  = time.time()
            if self.sm.strips:
                self._all_pixels = [
                    self._effect_pixels(i, s['pixels'], t)
                    for i, s in enumerate(self.sm.strips)
                ]
                self._universe_data = self.sm.send_all(self._all_pixels)
            await asyncio.sleep(max(0.0, frame - (time.monotonic() - t0)))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ── WebSocket payload ──

    def ws_payload(self, max_pixels_per_strip: int = 300) -> dict:
        strips_out = []
        for i, s in enumerate(self.sm.strips):
            pix = self._all_pixels[i] if i < len(self._all_pixels) else []
            n = len(pix)
            if n > max_pixels_per_strip:
                step = n / max_pixels_per_strip
                pix = [pix[int(j * step)] for j in range(max_pixels_per_strip)]
            # flatten to [r,g,b,r,g,b,...] to reduce JSON size
            flat = [c for px in pix for c in px]
            strips_out.append({'name': s['name'], 'pixels': flat})
        return {
            'universes': {str(k): v for k, v in self._universe_data.items()},
            'strips': strips_out,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Global state
# ──────────────────────────────────────────────────────────────────────────────

sm       = StripManager()
engine   = AnimationEngine(sm)
ws_peers: set = set()

FONTS_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')


# ──────────────────────────────────────────────────────────────────────────────
# HTTP / WebSocket handlers
# ──────────────────────────────────────────────────────────────────────────────

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=HTML_TEMPLATE, content_type='text/html')


async def handle_get_config(request: web.Request) -> web.Response:
    return web.json_response({
        'ip':          sm.artnet_ip,
        'strips':      sm.strips,
        'universe_map': sm.get_universe_map(),
    })


async def handle_post_config(request: web.Request) -> web.Response:
    data = await request.json()
    raw  = data.get('strips', [])
    strips = [
        {
            'name':            s.get('name', f'Strip {i + 1}'),
            'pixels':          max(1, int(s.get('pixels', 1))),
            'universe_offset': max(0, int(s.get('universe_offset', 0))),
            'start_channel':   max(1, min(512, int(s.get('start_channel', 1)))),
        }
        for i, s in enumerate(raw)
    ]
    sm.update_config(data.get('ip', '2.0.0.1'), strips)
    return web.json_response({'ok': True, 'universe_map': sm.get_universe_map()})


async def handle_post_effect(request: web.Request) -> web.Response:
    data = await request.json()
    if 'effect' in data:
        engine.effect = data['effect']
    if 'color' in data:
        c = data['color']
        engine.color = (int(c[0]), int(c[1]), int(c[2]))
    if 'brightness' in data:
        engine.brightness = max(0.0, min(1.0, float(data['brightness'])))
    if 'speed' in data:
        engine.speed = max(0.0, min(1.0, float(data['speed'])))
    return web.json_response({'ok': True})


async def handle_font(request: web.Request) -> web.Response:
    import os as _os
    name = request.match_info['name']
    if not name.endswith(('.otf', '.ttf', '.woff', '.woff2')):
        raise web.HTTPNotFound()
    path = _os.path.join(FONTS_DIR, name)
    if not _os.path.isfile(path):
        raise web.HTTPNotFound()
    ct = 'font/ttf' if name.endswith('.ttf') else 'font/otf'
    with open(path, 'rb') as f:
        return web.Response(body=f.read(), content_type=ct,
                            headers={'Cache-Control': 'public, max-age=86400'})


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ws_peers.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        ws_peers.discard(ws)
    return ws


# ──────────────────────────────────────────────────────────────────────────────
# Broadcast task  —  push DMX + pixel state to all WebSocket clients
# ──────────────────────────────────────────────────────────────────────────────

async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(0.1)   # 10 fps UI refresh
        if not ws_peers:
            continue
        payload = json.dumps(engine.ws_payload())
        dead = set()
        for ws in list(ws_peers):
            try:
                await ws.send_str(payload)
            except Exception:
                dead.add(ws)
        ws_peers.difference_update(dead)


# ──────────────────────────────────────────────────────────────────────────────
# App factory + startup
# ──────────────────────────────────────────────────────────────────────────────

async def _on_startup(app: web.Application) -> None:
    engine.start()
    asyncio.get_event_loop().create_task(_broadcast_loop())


def create_app(open_browser_port: int | None = None) -> web.Application:
    app = web.Application()
    app.router.add_get('/',              handle_root)
    app.router.add_get('/api/config',    handle_get_config)
    app.router.add_post('/api/config',   handle_post_config)
    app.router.add_post('/api/effect',   handle_post_effect)
    app.router.add_get('/fonts/{name}',  handle_font)
    app.router.add_get('/ws',            handle_ws)
    app.on_startup.append(_on_startup)

    if open_browser_port:
        async def _open(app: web.Application) -> None:
            await asyncio.sleep(0.6)
            webbrowser.open(f'http://localhost:{open_browser_port}')
        app.on_startup.append(_open)

    return app


def main() -> None:
    import os as _os
    global FONTS_DIR
    parser = argparse.ArgumentParser(description='LED Strip Tester — Art-Net controller')
    parser.add_argument('--port',       type=int, default=8080, help='Web UI port (default 8080)')
    parser.add_argument('--no-browser', action='store_true',    help='Do not open browser automatically')
    parser.add_argument('--fonts-dir',  default=None,           help='Path to Estúdio AB font files directory')
    args = parser.parse_args()

    if args.fonts_dir:
        FONTS_DIR = args.fonts_dir

    browser_port = None if args.no_browser else args.port
    app = create_app(open_browser_port=browser_port)

    fonts_ok = _os.path.isdir(FONTS_DIR)
    print(f'\n  LED Strip Tester  →  http://localhost:{args.port}')
    print(f'  Fonts: {FONTS_DIR} {"✓" if fonts_ok else "(not found — system fonts will be used)"}\n')
    web.run_app(app, host='0.0.0.0', port=args.port, print=None)


# ──────────────────────────────────────────────────────────────────────────────
# Embedded HTML / CSS / JS  (single-page app, no external dependencies)
# ──────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LED Strip Tester — Estúdio AB</title>
<style>
/* ── Webfonts (served locally via /fonts/) ── */
@font-face{font-family:'Neue Haas Grotesk Display';font-weight:400;font-style:normal;font-display:swap;src:url('/fonts/NeueHaasGrotDisp-55Roman-Trial.otf') format('opentype')}
@font-face{font-family:'Neue Haas Grotesk Display';font-weight:500;font-style:normal;font-display:swap;src:url('/fonts/NeueHaasGrotDisp-65Medium-Trial.otf') format('opentype')}
@font-face{font-family:'Neue Haas Grotesk Display';font-weight:700;font-style:normal;font-display:swap;src:url('/fonts/NeueHaasGrotDisp-75Bold-Trial.otf') format('opentype')}
@font-face{font-family:'Neue Haas Grotesk Display';font-weight:900;font-style:normal;font-display:swap;src:url('/fonts/NeueHaasGrotDisp-95Black-Trial.otf') format('opentype')}
@font-face{font-family:'Calling Code';font-weight:400;font-style:normal;font-display:swap;src:url('/fonts/CallingCode-Regular.otf') format('opentype')}
@font-face{font-family:'Calling Code';font-weight:700;font-style:normal;font-display:swap;src:url('/fonts/CallingCode-Bold.ttf') format('truetype')}

:root {
  /* ── Estúdio AB — palette tokens ── */
  --ab-ink:    #222223;
  --ab-bark:   #453B32;
  --ab-sage:   #B7BAAF;
  --ab-bone:   #EDE5D3;
  --ab-ember:  #BF4128;
  --ab-amber:  #EEA244;
  --ab-amber-lt:#F5C97A;
  --ab-steel:  #4B657E;
  --ab-plum:   #582D40;
  --ab-moss:   #89993E;
  --ab-garnet: #8A1D33;

  /* ── App surfaces (dark / ink mode) ── */
  --bg:     #222223;
  --surf:   #272421;
  --surf2:  #2d2a27;
  --border: rgba(237,229,211,0.13);
  --border-hi: rgba(237,229,211,0.38);
  --text:   #EDE5D3;
  --dim:    #B7BAAF;
  --acc:    #EEA244;
  --acc-lt: #F5C97A;
  --green:  #89993E;
  --red:    #BF4128;

  /* ── Typography ── */
  --sans: 'Neue Haas Grotesk Display', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  --mono: 'Calling Code', 'Sometype Mono', ui-monospace, monospace;

  /* ── Identity ── */
  --r:  0px;   /* square corners — brand rule */
  --ri: 2px;   /* inputs only */

  /* ── Motion ── */
  --ease: cubic-bezier(0.22, 1, 0.36, 1);
  --dur:  240ms;

  /* ── Signature gradient ── */
  --gradient-sig:
    radial-gradient(70% 180% at -5% 70%, var(--ab-steel)  0%, transparent 55%),
    radial-gradient(50% 180% at -2% 20%, var(--ab-plum)   0%, transparent 60%),
    radial-gradient(45% 180% at 104% 50%,var(--ab-amber)  0%, transparent 55%),
    radial-gradient(80% 120% at 55% 130%,var(--ab-moss)   0%, transparent 75%),
    var(--ab-sage);
}

/* ── Reset & base ── */
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}

/* ── Header ── */
.hdr{
  display:flex;align-items:center;gap:16px;
  padding:0 26px;height:66px;
  background:var(--surf);border-bottom:1px solid var(--border);
  flex-shrink:0;position:relative;
}
.hdr::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--gradient-sig);
}
.hdr-logo{display:inline-block;width:28px;height:28px;color:var(--text);flex-shrink:0;line-height:0}
.hdr-logo svg{display:block;width:100%;height:100%}
.hdr-sep{width:1px;height:20px;background:var(--border-hi)}
.hdr-studio{font-family:var(--mono);font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:var(--dim)}
.hdr-tool{font-family:var(--mono);font-size:15px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--text)}
.hdr-ip{font-family:var(--mono);font-size:13px;color:var(--dim);letter-spacing:0.04em}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.hdr-status{font-family:var(--mono);font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--red);transition:background .4s}
.dot.on{background:var(--green)}

/* ── Layout ── */
.main{display:flex;flex:1;overflow:hidden}
.sidebar{
  width:340px;flex-shrink:0;
  background:var(--surf);border-right:1px solid var(--border);
  overflow-y:auto;padding:16px;
  display:flex;flex-direction:column;gap:12px;
}
.rhs{flex:1;display:flex;flex-direction:column;overflow:hidden}
.rhs-scroll{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}

/* ── Sections ── */
.sec{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r);padding:16px}
.sec-hd{
  font-family:var(--mono);font-size:10px;font-weight:400;
  letter-spacing:0.12em;text-transform:uppercase;
  color:var(--dim);margin-bottom:14px;
  padding-bottom:8px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}

/* ── Form ── */
.frow{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.frow:last-child{margin-bottom:0}
.flbl{
  font-family:var(--mono);font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
  color:var(--dim);min-width:36px;
}
input[type=text],input[type=number]{
  font-family:var(--mono);font-size:13px;
  padding:9px 12px;border:1px solid rgba(237,229,211,0.2);
  background:transparent;color:var(--text);
  border-radius:var(--ri);flex:1;outline:none;
  transition:border-color var(--dur) var(--ease);
}
input:focus{border-color:rgba(237,229,211,0.65);box-shadow:0 0 0 3px rgba(237,229,211,0.07)}

/* ── Buttons — design system spec ── */
.btn{
  font-family:var(--mono);font-size:11px;font-weight:700;
  letter-spacing:0.1em;text-transform:uppercase;
  padding:9px 18px;border:1px solid var(--border-hi);border-radius:var(--r);
  background:transparent;color:var(--text);cursor:pointer;
  transition:all var(--dur) var(--ease);
}
.btn:hover{background:var(--text);color:var(--bg)}
.btn-pri{background:var(--text);color:var(--bg);border-color:var(--text)}
.btn-pri:hover{background:transparent;color:var(--text);border-color:var(--text)}
.btn-out{color:var(--dim);border-color:var(--border)}
.btn-out:hover{border-color:var(--acc);color:var(--acc)}
.btn-del{border:1px solid transparent;color:rgba(237,229,211,0.28);padding:4px 8px}
.btn-del:hover{border-color:var(--red);color:var(--red)}
.btn-sm{padding:6px 14px;font-size:10px}
.btn-row{display:flex;gap:8px;margin-top:12px}

/* ── Strip list ── */
.strip-list{display:flex;flex-direction:column;gap:8px;margin-bottom:2px}
.strip-item{
  display:flex;flex-direction:column;gap:6px;
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r);padding:10px 12px;
}
.strip-row{display:flex;align-items:center;gap:6px}
.strip-item input[type=text]{flex:1;min-width:0}
.strip-item input[type=number]{width:72px;flex:none}
.strip-item .num-sm{width:64px !important}
.px-lbl{font-family:var(--mono);font-size:10px;letter-spacing:0.08em;color:var(--dim)}

/* ── Universe map ── */
.umap{display:flex;flex-direction:column;gap:3px}
.umap-row{
  display:flex;gap:12px;align-items:flex-start;
  padding:6px 10px;border-left:2px solid var(--ab-bark);
}
.u-tag{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--acc);min-width:28px;letter-spacing:0.06em}
.u-entries{display:flex;flex-direction:column;gap:2px}
.u-entry{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:0.02em}
.u-entry span{color:var(--text)}

/* ── Color ── */
.color-area{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
input[type=color]{
  width:52px;height:52px;border:1px solid var(--border);border-radius:var(--r);
  cursor:pointer;padding:0;background:none;flex-shrink:0;
  transition:border-color var(--dur) var(--ease);
}
input[type=color]:hover{border-color:var(--border-hi)}
.swatches{display:flex;gap:6px;flex-wrap:wrap}
.sw{
  width:24px;height:24px;border-radius:var(--r);cursor:pointer;
  border:1px solid transparent;
  transition:transform 140ms var(--ease),border-color 140ms var(--ease);flex-shrink:0;
}
.sw:hover{transform:scale(1.18);border-color:rgba(237,229,211,0.45)}
.sw.active{border-color:var(--text);border-width:2px}

/* ── Sliders ── */
.slider-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.slider-row:last-child{margin-bottom:0}
.slbl{font-family:var(--mono);font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--dim);min-width:70px}
.sval{font-family:var(--mono);font-size:12px;color:var(--acc);min-width:36px;text-align:right}
input[type=range]{flex:1;accent-color:var(--acc);cursor:pointer;height:3px}

/* ── Effects grid ── */
.eff-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(106px,1fr));gap:5px}
.eff-btn{
  padding:10px 8px;border:1px solid var(--border);border-radius:var(--r);
  background:transparent;color:var(--dim);cursor:pointer;text-align:center;
  font-family:var(--mono);font-size:11px;font-weight:700;
  letter-spacing:0.06em;text-transform:uppercase;
  transition:all var(--dur) var(--ease);
  display:flex;flex-direction:column;align-items:center;gap:5px;
}
.eff-btn:hover{border-color:rgba(237,229,211,0.35);color:var(--text)}
.eff-btn.active{border-color:var(--acc);background:rgba(238,162,68,.07);color:var(--acc)}
.eff-icon{font-size:18px;line-height:1}

/* ── Strip preview ── */
.prev-panel{
  padding:12px 18px 14px;border-top:1px solid var(--border);
  flex-shrink:0;background:var(--surf);
}
.prev-hd{
  font-family:var(--mono);font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
  color:var(--dim);margin-bottom:10px;
}
.prev-rows{display:flex;flex-direction:column;gap:6px}
.prev-row{display:flex;align-items:center;gap:10px}
.prev-name{font-family:var(--mono);font-size:11px;letter-spacing:0.03em;color:var(--dim);min-width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prev-wrap{flex:1;height:16px;border-radius:var(--r);overflow:hidden;border:1px solid var(--border)}
canvas.pcanvas{display:block;width:100%;height:100%;image-rendering:pixelated}

/* ── DMX Monitor ── */
.mon-panel{padding:0 18px 14px;border-top:1px solid var(--border);flex-shrink:0;background:var(--surf)}
.mon-tabs{display:flex;gap:4px;padding:10px 0 8px}
.mon-tab{
  font-family:var(--mono);font-size:11px;letter-spacing:0.08em;text-transform:uppercase;
  padding:5px 14px;border:1px solid var(--border);border-radius:var(--r);
  background:transparent;color:var(--dim);cursor:pointer;
  transition:all var(--dur) var(--ease);
}
.mon-tab.active{background:var(--ab-bark);border-color:var(--ab-bark);color:var(--acc)}
.mon-empty{font-family:var(--mono);font-size:12px;color:var(--dim);padding:4px 0}
.mon-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));
  gap:3px;max-height:140px;overflow-y:auto;
}
.dcell{
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r);
  padding:5px 8px;font-family:var(--mono);font-size:12px;
  display:flex;justify-content:space-between;
}
.dch{color:var(--dim)}.dval{color:var(--text)}.dval.hi{color:var(--acc)}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(237,229,211,0.16);border-radius:0}
::-webkit-scrollbar-thumb:hover{background:rgba(237,229,211,0.3)}
</style>
</head>
<body>

<!-- ── Header ── -->
<div class="hdr">
  <span class="hdr-logo"><svg viewBox="0 0 5000 5000" fill="currentColor" fill-rule="evenodd" aria-hidden="true"><polygon points="818 1888.36 818 2500 1735.45 2500 1735.45 2805.82 818 3723.27 818 2500 206.36 2500 206.36 4334.91 818 4334.91 1123.82 4334.91 1735.45 3723.27 1735.45 4334.91 2347.09 4334.91 2347.09 2500 2347.09 1888.36 818 1888.36"/><polygon points="4182 2500 4182 3723.27 3264.55 2805.82 3264.55 2500 4182 2500 4182 1888.36 3264.55 1888.36 3264.55 665.09 2652.91 665.09 2652.91 4334.91 3264.55 4334.91 3264.55 3723.27 3876.18 4334.91 4182 4334.91 4793.64 4334.91 4793.64 2500 4182 2500"/></svg></span>
  <div class="hdr-sep"></div>
  <span class="hdr-studio">Estúdio AB</span>
  <div class="hdr-sep"></div>
  <span class="hdr-tool">LED Strip Tester</span>
  <span class="hdr-ip" id="hdr-ip"></span>
  <div class="hdr-right">
    <span class="hdr-status" id="ws-lbl">Connecting</span>
    <div class="dot" id="ws-dot"></div>
  </div>
</div>

<!-- ── Main ──────────────────────────────────── -->
<div class="main">

  <!-- ── Sidebar ── -->
  <div class="sidebar">

    <div class="sec">
      <div class="sec-hd">Art-Net Config</div>
      <div class="frow"><span class="flbl">IP</span>
        <input type="text" id="ip" value="2.0.0.1" placeholder="192.168.1.100"></div>
      <div class="frow"><span class="flbl">Port</span>
        <input type="number" id="port" value="6454" min="1" max="65535" style="max-width:90px"></div>
    </div>

    <div class="sec">
      <div class="sec-hd">LED Strips</div>
      <div class="strip-list" id="strip-list"></div>
      <div class="btn-row">
        <button class="btn btn-out btn-sm" onclick="addStrip()">+ Add Strip</button>
        <button class="btn btn-pri btn-sm" onclick="applyConfig()">Apply</button>
      </div>
    </div>

    <div class="sec">
      <div class="sec-hd">Universe Map</div>
      <div class="umap" id="umap">
        <span style="color:var(--dim);font-size:11px">Configure strips and press Apply</span>
      </div>
    </div>

  </div><!-- /sidebar -->

  <!-- ── Right side ── -->
  <div class="rhs">
    <div class="rhs-scroll">

      <div class="sec">
        <div class="sec-hd">Color</div>
        <div class="color-area">
          <input type="color" id="cpick" value="#ff0000" oninput="onColor(this.value)">
          <div class="swatches" id="swatches"></div>
        </div>
      </div>

      <div class="sec">
        <div class="sec-hd">Controls</div>
        <div class="slider-row">
          <span class="slbl">Brightness</span>
          <input type="range" id="sl-brightness" min="0" max="100" value="100"
            oninput="onSlider('brightness',this.value)">
          <span class="sval" id="v-brightness">100%</span>
        </div>
        <div class="slider-row">
          <span class="slbl">Speed</span>
          <input type="range" id="sl-speed" min="0" max="100" value="50"
            oninput="onSlider('speed',this.value)">
          <span class="sval" id="v-speed">50%</span>
        </div>
      </div>

      <div class="sec">
        <div class="sec-hd">Effects</div>
        <div class="eff-grid" id="eff-grid"></div>
      </div>

    </div><!-- /rhs-scroll -->

    <!-- ── Strip Preview ── -->
    <div class="prev-panel">
      <div class="prev-hd">Strip Preview</div>
      <div class="prev-rows" id="prev-rows">
        <span style="color:var(--dim);font-size:11px">No strips configured</span>
      </div>
    </div>

    <!-- ── DMX Monitor ── -->
    <div class="mon-panel">
      <div class="mon-tabs" id="mon-tabs"></div>
      <div id="mon-body">
        <div class="mon-empty">No data — apply config and select an effect</div>
      </div>
    </div>

  </div><!-- /rhs -->
</div><!-- /main -->

<script>
// ── State ──────────────────────────────────────────────────────────
const EFFECTS = [
  {id:'solid',          icon:'▬',  name:'Solid'},
  {id:'breathing',      icon:'◉',  name:'Breathing'},
  {id:'rainbow_solid',  icon:'▨',  name:'Rainbow'},
  {id:'rainbow_wave',   icon:'≋',  name:'Rainbow Wave'},
  {id:'chase',          icon:'▷',  name:'Chase'},
  {id:'theater_chase',  icon:'⁝⁝', name:'Theater'},
  {id:'fire',           icon:'△',  name:'Fire'},
  {id:'twinkle',        icon:'✦',  name:'Twinkle'},
  {id:'strobe',         icon:'◈',  name:'Strobe'},
  {id:'color_wipe',     icon:'◧',  name:'Color Wipe'},
];

const PRESETS = [
  {name:'Red',        hex:'#ff0000'},
  {name:'Green',      hex:'#00ff00'},
  {name:'Blue',       hex:'#0000ff'},
  {name:'White',      hex:'#ffffff'},
  {name:'Warm White', hex:'#ffb347'},
  {name:'Cyan',       hex:'#00ffff'},
  {name:'Magenta',    hex:'#ff00ff'},
  {name:'Yellow',     hex:'#ffff00'},
  {name:'Orange',     hex:'#ff6600'},
  {name:'Purple',     hex:'#8800ff'},
];

const S = {
  strips:  [{name:'Strip 1', pixels:150, universe_offset:0, start_channel:1}],
  ip:      '2.0.0.1',
  effect:  'rainbow_wave',
  color:   '#ff0000',
  brightness: 100,
  speed:   50,
  activeU: 0,
};

// ── Build effects grid ──────────────────────────────────────────────
function buildEffects() {
  const g = document.getElementById('eff-grid');
  g.innerHTML = '';
  EFFECTS.forEach(e => {
    const d = document.createElement('div');
    d.className = 'eff-btn' + (S.effect === e.id ? ' active' : '');
    d.dataset.id = e.id;
    d.innerHTML = `<span class="eff-icon">${e.icon}</span><span>${e.name}</span>`;
    d.onclick = () => setEffect(e.id);
    g.appendChild(d);
  });
}

// ── Build color swatches ────────────────────────────────────────────
function buildSwatches() {
  const c = document.getElementById('swatches');
  c.innerHTML = '';
  PRESETS.forEach(p => {
    const d = document.createElement('div');
    d.className = 'sw' + (S.color.toLowerCase() === p.hex ? ' active' : '');
    d.style.background = p.hex;
    d.title = p.name;
    d.onclick = () => pickColor(p.hex);
    c.appendChild(d);
  });
}

function updateSwatchActive() {
  document.querySelectorAll('.sw').forEach((s, i) => {
    s.classList.toggle('active', PRESETS[i].hex.toLowerCase() === S.color.toLowerCase());
  });
}

// ── Strip list render ───────────────────────────────────────────────
function renderStrips() {
  const el = document.getElementById('strip-list');
  el.innerHTML = '';
  S.strips.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'strip-item';
    row.innerHTML = `
      <div class="strip-row">
        <input type="text" value="${s.name}" placeholder="Name"
          oninput="S.strips[${i}].name=this.value">
        <input type="number" value="${s.pixels}" min="1" max="4096"
          oninput="S.strips[${i}].pixels=Math.max(1,parseInt(this.value)||1)">
        <span class="px-lbl">px</span>
        <button class="btn btn-del" onclick="removeStrip(${i})">✕</button>
      </div>
      <div class="strip-row">
        <span class="px-lbl">U</span>
        <input type="number" class="num-sm" value="${s.universe_offset||0}" min="0" max="32767"
          title="Universe offset"
          oninput="S.strips[${i}].universe_offset=Math.max(0,parseInt(this.value)||0)">
        <span class="px-lbl">ch</span>
        <input type="number" class="num-sm" value="${s.start_channel||1}" min="1" max="512"
          title="Start channel (1–512)"
          oninput="S.strips[${i}].start_channel=Math.max(1,Math.min(512,parseInt(this.value)||1))">
      </div>`;
    el.appendChild(row);
  });
}

function addStrip() {
  S.strips.push({name:`Strip ${S.strips.length+1}`, pixels:60, universe_offset:0, start_channel:1});
  renderStrips();
}
function removeStrip(i) {
  S.strips.splice(i, 1);
  renderStrips();
}

// ── Universe map render ─────────────────────────────────────────────
function renderUmap(map) {
  const el = document.getElementById('umap');
  if (!map || !map.length) {
    el.innerHTML = '<span style="color:var(--dim);font-size:11px">No strips configured</span>';
    return;
  }
  // group by universe
  const byU = {};
  map.forEach(strip => {
    strip.segments.forEach(seg => {
      (byU[seg.universe] = byU[seg.universe] || []).push(
        {strip: strip.name, cs: seg.ch_start, ce: seg.ch_end}
      );
    });
  });
  el.innerHTML = Object.keys(byU).sort((a,b)=>+a-+b).map(u => {
    const entries = byU[u].map(e =>
      `<div class="u-entry">${e.strip} <span>ch${e.cs}–${e.ce}</span></div>`
    ).join('');
    return `<div class="umap-row"><span class="u-tag">U${u}</span><div class="u-entries">${entries}</div></div>`;
  }).join('');
}

// ── Strip preview canvases ──────────────────────────────────────────
function rebuildPreviews() {
  const c = document.getElementById('prev-rows');
  if (!S.strips.length) {
    c.innerHTML = '<span style="color:var(--dim);font-size:11px">No strips configured</span>';
    return;
  }
  c.innerHTML = '';
  S.strips.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'prev-row';
    const wrap = document.createElement('div');
    wrap.className = 'prev-wrap';
    const cv = document.createElement('canvas');
    cv.className = 'pcanvas';
    cv.id = `cv${i}`;
    cv.width = Math.min(s.pixels, 512);
    cv.height = 1;
    wrap.appendChild(cv);
    row.innerHTML = `<span class="prev-name" title="${s.name}">${s.name}</span>`;
    row.appendChild(wrap);
    c.appendChild(row);
  });
}

function updatePreviews(strips) {
  if (!strips) return;
  strips.forEach((s, i) => {
    const cv = document.getElementById(`cv${i}`);
    if (!cv) return;
    const flat = s.pixels;           // [r,g,b,r,g,b,...]
    const n    = flat.length / 3;
    const w    = cv.width;
    const ctx  = cv.getContext('2d');
    const imgd = ctx.createImageData(w, 1);
    const buf  = imgd.data;
    const step = n / w;
    for (let x = 0; x < w; x++) {
      const idx = Math.floor(x * step) * 3;
      buf[x*4]   = flat[idx]   || 0;
      buf[x*4+1] = flat[idx+1] || 0;
      buf[x*4+2] = flat[idx+2] || 0;
      buf[x*4+3] = 255;
    }
    ctx.putImageData(imgd, 0, 0);
  });
}

// ── DMX Monitor ────────────────────────────────────────────────────
function updateMonitor(universes) {
  if (!universes) return;
  const keys = Object.keys(universes).map(Number).sort((a,b)=>a-b);
  if (!keys.length) return;
  if (!keys.includes(S.activeU)) S.activeU = keys[0];

  // Tabs
  const tabsEl = document.getElementById('mon-tabs');
  tabsEl.innerHTML = keys.map(u =>
    `<div class="mon-tab${u===S.activeU?' active':''}" onclick="setU(${u})">U${u}</div>`
  ).join('');

  // Channels
  const body  = document.getElementById('mon-body');
  const data  = universes[String(S.activeU)] || [];
  const cells = data.slice(0,512).map((v,i) =>
    `<div class="dcell"><span class="dch">${String(i+1).padStart(3,'0')}</span>`+
    `<span class="dval${v>0?' hi':''}">${v}</span></div>`
  ).join('');
  body.innerHTML = cells
    ? `<div class="mon-grid">${cells}</div>`
    : '<div class="mon-empty">Universe empty</div>';
}

function setU(u) {
  S.activeU = u;
}

// ── Effect / color / slider controls ───────────────────────────────
function setEffect(id) {
  S.effect = id;
  document.querySelectorAll('.eff-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.id === id));
  postEffect();
}

function pickColor(hex) {
  S.color = hex;
  document.getElementById('cpick').value = hex;
  updateSwatchActive();
  postEffect();
}

function onColor(hex) {
  S.color = hex;
  updateSwatchActive();
  postEffect();
}

function onSlider(type, val) {
  S[type] = parseInt(val);
  document.getElementById(`v-${type}`).textContent = val + '%';
  postEffect();
}

function postEffect() {
  const hex = S.color.replace('#','');
  const r = parseInt(hex.slice(0,2),16);
  const g = parseInt(hex.slice(2,4),16);
  const b = parseInt(hex.slice(4,6),16);
  fetch('/api/effect', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      effect: S.effect,
      color:  [r, g, b],
      brightness: S.brightness / 100,
      speed:  S.speed / 100,
    })
  }).catch(()=>{});
}

// ── Apply config ────────────────────────────────────────────────────
function applyConfig() {
  S.ip = document.getElementById('ip').value.trim();
  document.getElementById('hdr-ip').textContent = S.ip;
  fetch('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ip: S.ip, strips: S.strips})
  }).then(r=>r.json()).then(d => {
    renderUmap(d.universe_map);
    rebuildPreviews();
  }).catch(()=>{});
}

// ── WebSocket ────────────────────────────────────────────────────────
let ws, wsRetry = 1000;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    wsRetry = 1000;
    document.getElementById('ws-dot').classList.add('on');
    document.getElementById('ws-lbl').textContent = 'Connected';
  };
  ws.onclose = () => {
    document.getElementById('ws-dot').classList.remove('on');
    document.getElementById('ws-lbl').textContent = 'Reconnecting…';
    setTimeout(connectWS, wsRetry);
    wsRetry = Math.min(wsRetry * 1.5, 8000);
  };
  ws.onmessage = evt => {
    try {
      const d = JSON.parse(evt.data);
      if (d.universes) updateMonitor(d.universes);
      if (d.strips)    updatePreviews(d.strips);
    } catch(_) {}
  };
}

// ── Boot ────────────────────────────────────────────────────────────
(function init() {
  buildEffects();
  buildSwatches();
  renderStrips();

  fetch('/api/config').then(r=>r.json()).then(d => {
    if (d.ip) {
      S.ip = d.ip;
      document.getElementById('ip').value = d.ip;
      document.getElementById('hdr-ip').textContent = d.ip;
    }
    if (d.strips && d.strips.length) {
      S.strips = d.strips;
      renderStrips();
    }
    renderUmap(d.universe_map);
    rebuildPreviews();
  }).catch(()=>{});

  postEffect();
  connectWS();
})();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    main()
