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
        self.strips: list[dict] = []        # [{'name': str, 'pixels': int}]
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
        Strips are packed contiguously in the global channel space.
        170 RGB pixels = 510 channels fit cleanly; 512-ch universes mean
        a pixel can straddle a universe boundary — the map reflects raw channels.
        """
        result = []
        global_ch = 0
        for strip in self.strips:
            n = strip['pixels']
            if n == 0:
                result.append({'name': strip['name'], 'pixels': 0, 'segments': []})
                continue
            start_ch = global_ch
            end_ch = global_ch + n * 3 - 1
            segments = []
            for u in range(start_ch // 512, end_ch // 512 + 1):
                seg_s = max(start_ch, u * 512)
                seg_e = min(end_ch, (u + 1) * 512 - 1)
                segments.append({
                    'universe': u,
                    'ch_start': seg_s % 512 + 1,    # 1-indexed display
                    'ch_end':   seg_e % 512 + 1,
                    'channels': seg_e - seg_s + 1,
                })
            result.append({'name': strip['name'], 'pixels': n, 'segments': segments})
            global_ch += n * 3
        return result

    def send_all(self, all_pixels: list[list[tuple]]) -> dict[int, list[int]]:
        """
        Flatten all pixel RGB values into a linear channel buffer, split by
        universe (512 ch each), and fire Art-Net packets.
        Returns {universe: [ch_values]} for the DMX monitor.
        """
        channels: list[int] = []
        for i, strip in enumerate(self.strips):
            pix = all_pixels[i] if i < len(all_pixels) else []
            for r, g, b in pix:
                channels += [r, g, b]

        if not channels:
            return {}

        sender = self._sender_for()
        universe_data: dict[int, list[int]] = {}

        for base in range(0, len(channels), 512):
            chunk = channels[base:base + 512]
            u = base // 512
            universe_data[u] = chunk
            data = bytes(chunk)
            if len(data) % 2:
                data += b'\x00'
            sender.send_universe(u, data)

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
            'name':   s.get('name', f'Strip {i + 1}'),
            'pixels': max(1, int(s.get('pixels', 1))),
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
    app.router.add_get('/',            handle_root)
    app.router.add_get('/api/config',  handle_get_config)
    app.router.add_post('/api/config', handle_post_config)
    app.router.add_post('/api/effect', handle_post_effect)
    app.router.add_get('/ws',          handle_ws)
    app.on_startup.append(_on_startup)

    if open_browser_port:
        async def _open(app: web.Application) -> None:
            await asyncio.sleep(0.6)
            webbrowser.open(f'http://localhost:{open_browser_port}')
        app.on_startup.append(_open)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description='LED Strip Tester — Art-Net controller')
    parser.add_argument('--port',       type=int, default=8080, help='Web UI port (default 8080)')
    parser.add_argument('--no-browser', action='store_true',    help='Do not open browser automatically')
    args = parser.parse_args()

    browser_port = None if args.no_browser else args.port
    app = create_app(open_browser_port=browser_port)

    print(f'\n  LED Strip Tester  →  http://localhost:{args.port}\n')
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,700;1,9..40,300&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  /* Estúdio AB — paleta principal */
  --bg:     #121110;
  --surf:   #1c1a17;
  --surf2:  #242119;
  --border: #383229;
  --text:   #ede5d3;   /* creme */
  --dim:    #b7baaf;   /* sage */
  --acc:    #eea244;   /* âmbar */
  --acc-lt: #f5c97a;
  --brick:  #bf4128;
  --olive:  #89993e;
  --steel:  #4b657e;
  --wine:   #8a1d33;
  --brown:  #453b32;
  --green:  #a8b86b;   /* olive-green para status ok */
  --red:    #bf4128;
  --r:      6px;
  --font:   'DM Sans', 'Neue Haas Grotesk', 'Helvetica Neue', Helvetica, system-ui, sans-serif;
  --mono:   'JetBrains Mono', 'Calling Code', 'Courier New', monospace;
}

/* ── Reset & base ── */
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;line-height:1.5;display:flex;flex-direction:column}

/* ── Header ── */
.hdr{
  display:flex;align-items:center;gap:12px;
  padding:0 20px;height:54px;
  background:var(--surf);border-bottom:1px solid var(--border);
  flex-shrink:0;position:relative;
}
.hdr::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--wine) 0%,var(--brown) 22%,var(--steel) 45%,var(--olive) 65%,var(--acc) 85%,var(--dim) 100%);
}
.hdr-studio{font-size:9px;font-weight:500;letter-spacing:3px;text-transform:uppercase;color:var(--dim)}
.hdr-sep{width:1px;height:14px;background:var(--border)}
.hdr-tool{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--text)}
.hdr-ip{font-family:var(--mono);font-size:11px;color:var(--dim)}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.hdr-status{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--red);transition:background .4s}
.dot.on{background:var(--green)}

/* ── Layout ── */
.main{display:flex;flex:1;overflow:hidden}
.sidebar{
  width:292px;flex-shrink:0;
  background:var(--surf);border-right:1px solid var(--border);
  overflow-y:auto;padding:15px;
  display:flex;flex-direction:column;gap:11px;
}
.rhs{flex:1;display:flex;flex-direction:column;overflow:hidden}
.rhs-scroll{flex:1;overflow-y:auto;padding:15px;display:flex;flex-direction:column;gap:11px}

/* ── Sections ── */
.sec{background:var(--surf2);border:1px solid var(--border);border-radius:var(--r);padding:14px}
.sec-hd{
  font-size:9px;font-weight:700;letter-spacing:3px;text-transform:uppercase;
  color:var(--acc);margin-bottom:13px;
  display:flex;align-items:center;gap:9px;
}
.sec-hd::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── Form ── */
.frow{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.frow:last-child{margin-bottom:0}
.flbl{
  font-size:9px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--dim);min-width:36px;
}
input[type=text],input[type=number]{
  background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:6px 10px;border-radius:5px;font-size:12px;font-family:var(--font);
  flex:1;outline:none;transition:border-color .2s;
}
input:focus{border-color:var(--acc)}

/* ── Buttons ── */
.btn{
  padding:6px 14px;border-radius:5px;border:none;cursor:pointer;
  font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
  font-family:var(--font);transition:all .15s;
}
.btn-pri{background:var(--acc);color:var(--bg)}.btn-pri:hover{background:var(--acc-lt)}
.btn-out{background:transparent;border:1px solid var(--border);color:var(--dim)}
.btn-out:hover{border-color:var(--acc);color:var(--acc)}
.btn-del{background:transparent;border:1px solid transparent;color:#4a4540;padding:4px 8px}
.btn-del:hover{border-color:var(--brick);color:var(--brick)}
.btn-sm{padding:5px 11px;font-size:9px}
.btn-row{display:flex;gap:8px;margin-top:10px}

/* ── Strip list ── */
.strip-list{display:flex;flex-direction:column;gap:6px;margin-bottom:2px}
.strip-item{
  display:flex;align-items:center;gap:6px;
  background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:5px 9px;
}
.strip-item input[type=text]{max-width:86px}
.strip-item input[type=number]{width:62px;flex:none}
.px-lbl{font-size:9px;letter-spacing:.5px;color:var(--dim)}

/* ── Universe map ── */
.umap{display:flex;flex-direction:column;gap:4px}
.umap-row{
  display:flex;gap:10px;align-items:flex-start;
  background:var(--bg);border-radius:4px;padding:5px 8px;
  border-left:2px solid var(--brown);
}
.u-tag{font-family:var(--mono);font-size:10px;font-weight:500;color:var(--acc);min-width:22px}
.u-entries{display:flex;flex-direction:column;gap:2px}
.u-entry{font-family:var(--mono);font-size:10px;color:var(--dim)}
.u-entry span{color:var(--text)}

/* ── Color ── */
.color-area{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
input[type=color]{
  width:52px;height:52px;border:1px solid var(--border);border-radius:var(--r);
  cursor:pointer;padding:0;background:none;flex-shrink:0;transition:border-color .2s;
}
input[type=color]:hover{border-color:var(--acc)}
.swatches{display:flex;gap:6px;flex-wrap:wrap}
.sw{
  width:24px;height:24px;border-radius:50%;cursor:pointer;
  border:2px solid transparent;transition:border-color .15s,transform .12s;flex-shrink:0;
}
.sw:hover{transform:scale(1.2);border-color:rgba(237,229,211,.35)}
.sw.active{border-color:var(--text)}

/* ── Sliders ── */
.slider-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.slider-row:last-child{margin-bottom:0}
.slbl{font-size:9px;font-weight:500;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);min-width:70px}
.sval{font-family:var(--mono);font-size:11px;color:var(--acc);min-width:36px;text-align:right}
input[type=range]{flex:1;accent-color:var(--acc);cursor:pointer;height:3px}

/* ── Effects grid ── */
.eff-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(106px,1fr));gap:6px}
.eff-btn{
  padding:10px 8px;border-radius:5px;border:1px solid var(--border);
  background:var(--bg);color:var(--dim);cursor:pointer;text-align:center;
  font-size:11px;font-weight:600;font-family:var(--font);
  transition:all .15s;display:flex;flex-direction:column;align-items:center;gap:5px;
}
.eff-btn:hover{border-color:var(--acc);color:var(--text)}
.eff-btn.active{
  border-color:var(--acc);background:rgba(238,162,68,.09);color:var(--acc);
}
.eff-icon{font-size:17px;line-height:1}

/* ── Strip preview ── */
.prev-panel{
  padding:10px 16px 12px;border-top:1px solid var(--border);
  flex-shrink:0;background:var(--surf);
}
.prev-hd{
  font-size:9px;font-weight:700;letter-spacing:3px;text-transform:uppercase;
  color:var(--acc);margin-bottom:9px;
}
.prev-rows{display:flex;flex-direction:column;gap:6px}
.prev-row{display:flex;align-items:center;gap:10px}
.prev-name{font-size:10px;letter-spacing:.3px;color:var(--dim);min-width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prev-wrap{flex:1;height:16px;border-radius:3px;overflow:hidden;border:1px solid var(--border)}
canvas.pcanvas{display:block;width:100%;height:100%;image-rendering:pixelated}

/* ── DMX Monitor ── */
.mon-panel{padding:0 16px 12px;border-top:1px solid var(--border);flex-shrink:0;background:var(--surf)}
.mon-tabs{display:flex;gap:4px;padding:9px 0 7px}
.mon-tab{
  padding:3px 12px;border-radius:4px;background:transparent;
  border:1px solid var(--border);color:var(--dim);cursor:pointer;
  font-family:var(--mono);font-size:10px;letter-spacing:.5px;transition:all .15s;
}
.mon-tab.active{background:var(--brown);border-color:var(--brown);color:var(--acc)}
.mon-empty{font-size:11px;color:var(--dim);padding:4px 0;font-style:italic}
.mon-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));
  gap:2px;max-height:84px;overflow-y:auto;
}
.dcell{
  background:var(--bg);border-radius:3px;padding:3px 6px;
  font-family:var(--mono);font-size:10px;
  display:flex;justify-content:space-between;border:1px solid var(--border);
}
.dch{color:var(--dim)}.dval{color:var(--text)}.dval.hi{color:var(--acc)}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--brown)}
</style>
</head>
<body>

<!-- ── Header ── -->
<div class="hdr">
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
  strips:  [{name:'Strip 1', pixels:150}],
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
      <input type="text" value="${s.name}" placeholder="Name"
        oninput="S.strips[${i}].name=this.value">
      <input type="number" value="${s.pixels}" min="1" max="4096"
        oninput="S.strips[${i}].pixels=Math.max(1,parseInt(this.value)||1)">
      <span class="px-lbl">px</span>
      <button class="btn btn-del" onclick="removeStrip(${i})">✕</button>`;
    el.appendChild(row);
  });
}

function addStrip() {
  S.strips.push({name:`Strip ${S.strips.length+1}`, pixels:60});
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
