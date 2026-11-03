#!/usr/bin/env python3
"""After a switch is reflashed (fw 2.00, network data wiped): open permit-join, wait for it
to announce, re-interview, confirm swBuildId, then start the 3.04 OTA in Zigbee2MQTT.

usage: Z2M_URL=ws://z2m-host:8080/api python z2m_rejoin_ota.py "Kitchen Switch" [wait_minutes]
"""
import asyncio, json, os, sys, time
import websockets

URL = os.environ.get("Z2M_URL", "ws://localhost:8080/api")
NAME = sys.argv[1]
WAIT = float(sys.argv[2]) * 60 if len(sys.argv) > 2 else 240


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def drain(ws, seconds):
    end = time.time() + seconds
    devices = None
    while time.time() < end:
        try:
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time())))
        except asyncio.TimeoutError:
            break
        if d.get("topic") == "bridge/devices":
            devices = d["payload"]
    return devices


async def wait_for(ws, pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time())))
        except asyncio.TimeoutError:
            break
        r = pred(d.get("topic", ""), d.get("payload"))
        if r is not None:
            return r
    return None


def is_log(t):
    return t in ("bridge/logging", "bridge/log")


async def main():
    async with websockets.connect(URL, max_size=2**26, ping_interval=20) as ws:
        devices = await drain(ws, 3)
        ieee = next((x["ieee_address"] for x in devices or [] if x.get("friendly_name") == NAME), None)
        if not ieee:
            log(f"'{NAME}' is not a known Z2M device"); return 1
        log(f"{NAME} = {ieee}; opening permit_join for 254 s, waiting up to {WAIT/60:.0f} min for it to announce")
        await ws.send(json.dumps({"topic": "bridge/request/permit_join", "payload": {"time": 254}}))

        deadline = time.time() + WAIT
        announced = False
        while time.time() < deadline and not announced:
            def pred(t, p):
                if t == "bridge/event" and isinstance(p, dict) and p.get("type") in ("device_announce", "device_joined") \
                        and p.get("data", {}).get("ieee_address") == ieee:
                    return p["type"]
                return None
            r = await wait_for(ws, pred, min(240, deadline - time.time()))
            if r:
                announced = True; log(f"{r} from {NAME}")
            elif time.time() < deadline:
                log("re-opening permit_join")
                await ws.send(json.dumps({"topic": "bridge/request/permit_join", "payload": {"time": 254}}))
        if not announced:
            log("gave up waiting; is the breaker on?"); return 2

        await asyncio.sleep(5)
        await ws.send(json.dumps({"topic": "bridge/request/device/interview", "payload": {"id": NAME}}))
        r = await wait_for(ws, lambda t, p: (p.get("data", {}).get("status") if t == "bridge/event" and isinstance(p, dict)
                                              and p.get("type") == "device_interview" and p["data"].get("ieee_address") == ieee
                                              and p["data"].get("status") in ("successful", "failed") else None), 90)
        log("interview:", r)

        await ws.send(json.dumps({"topic": "bridge/request/permit_join", "payload": {"time": 0}}))
        await ws.send(json.dumps({"topic": f"{NAME}/set", "payload": {"read": {"cluster": "genBasic", "attributes": ["swBuildId", "dateCode"]}}}))
        r = await wait_for(ws, lambda t, p: (p.get("message", "").split("genBasic':")[-1].strip()
                                              if is_log(t) and isinstance(p, dict) and "Read result of 'genBasic'" in p.get("message", "") else None), 20)
        log("genBasic:", r)

        await ws.send(json.dumps({"topic": "bridge/request/device/ota_update/check", "payload": {"id": NAME}}))
        r = await wait_for(ws, lambda t, p: p if t == "bridge/response/device/ota_update/check" else None, 60)
        log("ota check:", json.dumps(r)[:200] if r else None)
        if not (r and r.get("status") == "ok" and r["data"].get("update_available")):
            log("no update offered; stopping"); return 3

        await ws.send(json.dumps({"topic": "bridge/request/device/ota_update/update", "payload": {"id": NAME}}))
        r = await wait_for(ws, lambda t, p: (p.get("message") if is_log(t) and isinstance(p, dict)
                                              and NAME in p.get("message", "") and "OTA" in p.get("message", "")
                                              and "MQTT publish" not in p.get("message", "") else None), 30)
        log("ota:", r)
        log("OTA started; follow it with z2m_watch_ota.py")
        return 0


sys.exit(asyncio.run(main()))
