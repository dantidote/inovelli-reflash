"""Follow one device's OTA in Zigbee2MQTT: progress, related log lines, and the final result.

usage: Z2M_URL=ws://z2m-host:8080/api python -u z2m_watch_ota.py "<friendly name>" [seconds]
"""
import asyncio, json, os, sys, time
import websockets

URL = os.environ.get("Z2M_URL", "ws://localhost:8080/api")
if len(sys.argv) < 2:
    sys.exit(__doc__)
NAME = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 3 * 3600

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

async def session(deadline):
    last_pct = None
    async with websockets.connect(URL, max_size=2**26, ping_interval=20) as ws:
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=min(60, max(0.1, deadline - time.time())))
            except asyncio.TimeoutError:
                continue
            d = json.loads(msg); t = d.get("topic", ""); p = d.get("payload")
            if t in ("bridge/logging", "bridge/log"):
                m = p.get("message", "") if isinstance(p, dict) else ""
                if NAME in m and "MQTT publish" not in m:
                    log("LOG:", m[:240])
                    if "Finished update" in m or "Update of" in m and "failed" in m.lower():
                        return True
            elif t == "bridge/response/device/ota_update/update":
                log("RESPONSE:", json.dumps(p)[:400])
                return True
            elif t == NAME and isinstance(p, dict) and isinstance(p.get("update"), dict):
                u = p["update"]
                pct = u.get("progress")
                if pct is not None and pct != last_pct:
                    last_pct = pct
                    log(f"progress {pct}% remaining {u.get('remaining')}s state={u.get('state')}")
    return False

async def main():
    deadline = time.time() + DURATION
    log(f"watching OTA of {NAME} for up to {DURATION/60:.0f} min")
    while time.time() < deadline:
        try:
            if await session(deadline):
                break
        except Exception as e:
            log("reconnect after error:", repr(e)[:120])
            await asyncio.sleep(10)
    log("watcher done")

asyncio.run(main())
