#!/usr/bin/env python3
"""Batch rejoin + OTA for reflashed Inovelli VZM31-SN switches, driven over the Zigbee2MQTT websocket.

For every VZM31-SN not yet on 3.04 (or only the names given): keep permit-join open while it is
still off the network, probe it, re-interview it once it answers (refreshes the stale 2.18 database
entry and tells us whether it really runs 2.00), start the 3.04 OTA with bounded concurrency, and
run Z2M `configure` once it reaches 3.04 (the NVM wipe dropped its reporting bindings).

usage: Z2M_URL=ws://z2m-host:8080/api python -u z2m_batch_ota.py [max_concurrent_ota] [name ...]
(Z2M_URL is the Zigbee2MQTT frontend websocket; default ws://localhost:8080/api. Needs `pip install websockets`.)
"""
import asyncio, json, os, sys, time
import websockets

URL = os.environ.get("Z2M_URL", "ws://localhost:8080/api")
MAXC = int(sys.argv[1]) if len(sys.argv) > 1 else 4
ONLY = set(sys.argv[2:])
TARGET = 16909060          # 3.04
MAX_HOURS = 7
PROBE_EVERY = 120
PERMIT_EVERY = 200
PERMIT_FOR = 45 * 60       # stop re-opening permit-join after this long
OTA_ATTEMPTS = 3
FINAL = {"configured", "configure_failed", "stuck", "failed", "skipped"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Dev:
    def __init__(self, name, ieee):
        self.name, self.ieee = name, ieee
        self.phase = "pending"
        self.fw = None
        self.fw_at = 0
        self.installed = None
        self.progress = None
        self.progress_at = 0
        self.progress_mark = 0
        self.saw_updating = False
        self.last_probe = 0
        self.ota_started = None
        self.attempts = 0
        self.interviews = 0
        self.configures = 0
        self.not_before = 0
        self.queued_at = 0
        self.stall_warned = False
        self.note = ""
        self.interview_fut = None
        self.task = None


class Batch:
    def __init__(self):
        self.devs = {}
        self.ieee2dev = {}
        self.ws = None
        self.waiters = {}
        self.read_fut = None
        self.lock = asyncio.Lock()
        self.last_permit = 0
        self.permit_open = False
        self.started = time.time()
        self.last_status = 0
        self.frozen = False

    # ---- plumbing ---------------------------------------------------------
    def spawn(self, d, coro):
        d.task = asyncio.create_task(coro)

    async def send(self, topic, payload):
        await self.ws.send(json.dumps({"topic": topic, "payload": payload}))

    async def request(self, topic, payload, timeout):
        tx = f"b{time.time_ns() % 10**12}"
        fut = asyncio.get_running_loop().create_future()
        self.waiters[tx] = fut
        try:
            await self.send(topic, dict(payload, transaction=tx))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.waiters.pop(tx, None)

    def all_final(self):
        return self.frozen and all(d.phase in FINAL for d in self.devs.values())

    # ---- incoming messages ------------------------------------------------
    def on_devices(self, devices):
        now = time.time()
        for x in devices:
            model = (x.get("definition") or {}).get("model") or x.get("model_id")
            if model != "VZM31-SN":
                continue
            name, ieee = x["friendly_name"], x["ieee_address"]
            d = self.devs.get(name)
            if d is None:
                if self.frozen:
                    continue
                d = Dev(name, ieee)
                self.devs[name] = d
                self.ieee2dev[ieee] = d
            d.fw, d.fw_at = x.get("software_build_id"), now

    def on_state(self, d, p, initial):
        u = p.get("update") if isinstance(p.get("update"), dict) else None
        if u:
            if u.get("installed_version") is not None:
                d.installed = u["installed_version"]
            if u.get("state") == "updating":
                d.saw_updating = True
            pct = u.get("progress")
            if pct is not None and pct != d.progress:
                d.progress, d.progress_at = pct, time.time()
                if d.phase == "updating" and pct >= d.progress_mark + 25:
                    d.progress_mark = int(pct // 25) * 25
                    log(f"{d.name}: {pct:.0f}%  ({u.get('remaining')} s left)")
            if d.phase == "updating" and not initial:
                if u.get("state") == "idle" and d.installed == TARGET:
                    self.finish(d, True, "state idle at 3.04")
                elif u.get("state") == "available" and d.saw_updating and d.installed != TARGET \
                        and d.ota_started and time.time() - d.ota_started > 60:
                    self.finish(d, False, "Z2M dropped back to 'available'")
        if initial:
            if u and u.get("state") == "updating" and d.phase == "pending":
                d.phase, d.ota_started, d.saw_updating = "updating", time.time(), True
                d.attempts += 1
                log(f"{d.name}: adopting an OTA that is already running")
            return
        if d.phase == "pending":
            d.phase = "alive"
            log(f"{d.name}: answered (lqi {p.get('linkquality')})")
            self.spawn(d, self.interview(d))

    def on_event(self, p):
        t, data = p.get("type"), p.get("data") or {}
        d = self.ieee2dev.get(data.get("ieee_address")) or self.devs.get(data.get("friendly_name"))
        if not d:
            return
        if t in ("device_announce", "device_joined"):
            log(f"{d.name}: {t}")
            if d.phase == "pending":
                d.phase = "alive"
                self.spawn(d, self.interview(d, delay=6))
        elif t == "device_interview":
            st = data.get("status")
            if st in ("successful", "failed") and d.interview_fut and not d.interview_fut.done():
                d.interview_fut.set_result(st)
        elif t == "device_leave":
            log(f"{d.name}: left the network")

    def on_log(self, m):
        if "Read result of 'genBasic'" in m and self.read_fut and not self.read_fut.done():
            self.read_fut.set_result(m.split("genBasic':", 1)[1].strip())
            return
        for d in self.devs.values():
            q = f"'{d.name}'"
            if q not in m:
                continue
            if f"Finished update of {q}" in m:
                self.finish(d, True, "log: finished")
            elif f"Update of {q} failed" in m:
                self.finish(d, False, m.split("failed", 1)[1][:160])
            elif "OTA" in m and "MQTT publish" not in m and "available for" not in m:
                log("z2m:", m[:200])

    # ---- per-device workflows ---------------------------------------------
    async def interview(self, d, delay=0):
        try:
            if delay:
                await asyncio.sleep(delay)
            async with self.lock:
                if d.phase != "alive":
                    return
                d.interviews += 1
                t_req = time.time()
                d.interview_fut = asyncio.get_running_loop().create_future()
                r = await self.request("bridge/request/device/interview", {"id": d.name}, 15)
                if r and r.get("status") == "error":
                    log(f"{d.name}: interview request refused: {r.get('error')}")
                try:
                    st = await asyncio.wait_for(d.interview_fut, 90)
                except asyncio.TimeoutError:
                    st = "timeout"
                d.interview_fut = None
                if st != "successful":
                    log(f"{d.name}: interview {st}")
                    if d.interviews >= 3:
                        d.phase, d.note = "stuck", f"interview keeps failing ({st})"
                    else:
                        d.phase, d.last_probe = "pending", time.time()
                    return
                for _ in range(10):
                    if d.fw_at > t_req:
                        break
                    await asyncio.sleep(0.5)
                fw = d.fw if d.fw_at > t_req else None
                if fw is None:
                    fw = await self.read_fw(d)
                log(f"{d.name}: interviewed, swBuildId {fw}")
                self.classify(d, fw)
        except Exception as e:
            log(f"{d.name}: interview task error {e!r}; will re-probe")
            if d.phase == "alive":
                d.phase = "pending"

    async def read_fw(self, d):
        self.read_fut = asyncio.get_running_loop().create_future()
        await self.send(f"{d.name}/set", {"read": {"cluster": "genBasic", "attributes": ["swBuildId", "dateCode"]}})
        try:
            r = await asyncio.wait_for(self.read_fut, 20)
        except asyncio.TimeoutError:
            r = None
        self.read_fut = None
        try:
            return json.loads(r).get("swBuildId") if r else None
        except Exception:
            return None

    def classify(self, d, fw):
        if fw == "3.04" or d.installed == TARGET:
            d.phase = "finished"
            self.spawn(d, self.configure(d))
        elif fw == "2.00":
            d.phase, d.queued_at, d.not_before = "queued", time.time(), 0
        elif fw is None:
            if d.interviews >= 3:
                d.phase, d.note = "stuck", "firmware version unreadable"
            else:
                d.phase, d.last_probe = "pending", time.time()
        else:
            d.phase, d.note = "stuck", f"still on {fw}: reflash did not take"
            log(f"{d.name}: STILL ON {fw}; needs another pass on the Pi")

    async def start_ota(self, d):
        try:
            d.attempts += 1
            r = await self.request("bridge/request/device/ota_update/check", {"id": d.name}, 120)
            if not (r and r.get("status") == "ok" and (r.get("data") or {}).get("update_available")):
                log(f"{d.name}: OTA check -> {json.dumps(r)[:200] if r else 'timeout'}")
                if r and r.get("status") == "ok":
                    d.phase, d.note = "failed", "no update offered"
                elif d.attempts >= OTA_ATTEMPTS:
                    d.phase, d.note = "failed", "OTA check kept failing"
                else:
                    d.phase, d.not_before = "queued", time.time() + 60
                return
            d.ota_started, d.progress, d.progress_mark, d.saw_updating = time.time(), None, 0, False
            d.stall_warned, d.phase = False, "updating"
            log(f"{d.name}: OTA started (attempt {d.attempts})")
            r = await self.request("bridge/request/device/ota_update/update", {"id": d.name}, 4 * 3600)
            if r is not None:
                self.finish(d, r.get("status") == "ok", f"response {json.dumps(r)[:160]}")
        except Exception as e:
            log(f"{d.name}: OTA task error {e!r}")
            if d.phase == "starting":
                d.phase, d.not_before = "queued", time.time() + 30

    def finish(self, d, ok, why):
        if d.phase != "updating":
            return
        took = (time.time() - d.ota_started) / 60 if d.ota_started else 0
        if ok:
            d.phase = "finished"
            log(f"{d.name}: OTA FINISHED in {took:.0f} min ({why})")
            self.spawn(d, self.configure(d, delay=30))
        else:
            log(f"{d.name}: OTA FAILED after {took:.0f} min ({why})")
            if d.attempts >= OTA_ATTEMPTS:
                d.phase, d.note = "failed", why
            else:
                d.phase, d.not_before = "queued", time.time() + 120

    async def configure(self, d, delay=0):
        try:
            if delay:
                await asyncio.sleep(delay)
            d.configures += 1
            r = await self.request("bridge/request/device/configure", {"id": d.name}, 150)
            if r and r.get("status") == "ok":
                d.phase = "configured"
                log(f"{d.name}: configured OK")
            elif d.configures < 2:
                log(f"{d.name}: configure failed ({(r or {}).get('error', 'timeout')}); retrying in 60 s")
                self.spawn(d, self.configure(d, delay=60))
            else:
                d.phase, d.note = "configure_failed", (r or {}).get("error", "timeout")
                log(f"{d.name}: configure FAILED: {d.note}")
        except Exception as e:
            log(f"{d.name}: configure task error {e!r}")

    # ---- scheduler --------------------------------------------------------
    async def tick(self):
        now = time.time()
        for d in self.devs.values():
            if d.phase in ("alive", "starting", "finished") and (d.task is None or d.task.done()):
                d.phase = {"alive": "pending", "starting": "queued", "finished": "finished"}[d.phase]
                if d.phase == "finished":
                    self.spawn(d, self.configure(d))
        pending = [d for d in self.devs.values() if d.phase == "pending"]
        if pending and now - self.started < PERMIT_FOR and now - self.last_permit > PERMIT_EVERY:
            await self.send("bridge/request/permit_join", {"time": 254})
            self.last_permit, self.permit_open = now, True
            log("permit_join open (254 s); still waiting for: " + ", ".join(sorted(d.name for d in pending)))
        if not pending and self.permit_open:
            await self.send("bridge/request/permit_join", {"time": 0})
            self.permit_open = False
            log("permit_join closed")
        for d in pending:
            if now - d.last_probe > PROBE_EVERY:
                d.last_probe = now
                await self.send(f"{d.name}/get", {"state": ""})
        running = sum(1 for d in self.devs.values() if d.phase in ("updating", "starting"))
        queued = sorted((d for d in self.devs.values() if d.phase == "queued" and now >= d.not_before),
                        key=lambda d: d.queued_at)
        for d in queued:
            if running >= MAXC:
                break
            d.phase = "starting"
            running += 1
            self.spawn(d, self.start_ota(d))
        for d in self.devs.values():
            if d.phase == "updating" and d.progress_at and now - d.progress_at > 20 * 60 and not d.stall_warned:
                d.stall_warned = True
                log(f"{d.name}: no OTA progress for 20 min (stuck at {d.progress}%)")
        if now - self.last_status > 300:
            self.last_status = now
            self.status()

    def status(self):
        by = {}
        for d in self.devs.values():
            if d.phase != "skipped":
                by.setdefault(d.phase, []).append(d)
        parts = []
        for ph, ds in sorted(by.items()):
            names = ", ".join(f"{d.name} {d.progress:.0f}%" if ph == "updating" and d.progress is not None else d.name
                              for d in sorted(ds, key=lambda d: d.name))
            parts.append(f"{ph}[{len(ds)}]: {names}")
        log("STATUS  " + "  |  ".join(parts))

    # ---- connection lifecycle ---------------------------------------------
    async def bootstrap(self):
        raw = []
        end = time.time() + 3
        while time.time() < end:
            try:
                raw.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=max(0.1, end - time.time()))))
            except asyncio.TimeoutError:
                break
        for m in raw:
            if m.get("topic") == "bridge/devices":
                self.on_devices(m["payload"])
        if not self.frozen:
            for m in raw:
                d = self.devs.get(m.get("topic"))
                if d and isinstance(m.get("payload"), dict):
                    u = m["payload"].get("update") or {}
                    d.installed = u.get("installed_version")
            for d in self.devs.values():
                if (ONLY and d.name not in ONLY) or (not ONLY and d.installed == TARGET):
                    d.phase = "skipped"
            self.frozen = True
            targets = sorted(d.name for d in self.devs.values() if d.phase != "skipped")
            log(f"targets ({len(targets)}), max {MAXC} concurrent OTAs: " + ", ".join(targets))
        for m in raw:
            d = self.devs.get(m.get("topic"))
            if d and d.phase != "skipped" and isinstance(m.get("payload"), dict):
                self.on_state(d, m["payload"], initial=True)

    async def recv_loop(self):
        async for msg in self.ws:
            try:
                m = json.loads(msg)
            except Exception:
                continue
            t, p = m.get("topic", ""), m.get("payload")
            if t == "bridge/devices":
                self.on_devices(p)
            elif t == "bridge/event" and isinstance(p, dict):
                self.on_event(p)
            elif t in ("bridge/logging", "bridge/log") and isinstance(p, dict):
                self.on_log(p.get("message", ""))
            elif t.startswith("bridge/response/"):
                fut = self.waiters.get(p.get("transaction")) if isinstance(p, dict) else None
                if fut and not fut.done():
                    fut.set_result(p)
            elif t in self.devs and isinstance(p, dict) and self.devs[t].phase != "skipped":
                self.on_state(self.devs[t], p, initial=False)

    async def tick_loop(self, deadline):
        while time.time() < deadline:
            await self.tick()
            if self.all_final():
                if self.permit_open:
                    await self.send("bridge/request/permit_join", {"time": 0})
                return
            await asyncio.sleep(2)

    async def run(self):
        deadline = self.started + MAX_HOURS * 3600
        while time.time() < deadline:
            try:
                async with websockets.connect(URL, max_size=2**26, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws
                    await self.bootstrap()
                    recv = asyncio.create_task(self.recv_loop())
                    ticker = asyncio.create_task(self.tick_loop(deadline))
                    done, _ = await asyncio.wait({recv, ticker}, return_when=asyncio.FIRST_COMPLETED)
                    for t in (recv, ticker):
                        if not t.done():
                            t.cancel()
                    for t in done:
                        t.result()
                    if ticker in done:
                        break
            except Exception as e:
                log("websocket error:", repr(e)[:160], "; reconnecting in 10 s")
                await asyncio.sleep(10)
        self.status()
        for d in sorted(self.devs.values(), key=lambda d: d.name):
            if d.note:
                log(f"  {d.name}: {d.phase}: {d.note}")
        log("batch done")


asyncio.run(Batch().run())
