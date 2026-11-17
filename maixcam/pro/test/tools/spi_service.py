#!/usr/bin/env python3
"""SPI owner and local lamp/thermal console service.

The KWS model runs in a separate process because it can starve the bit-banged
SPI4 bus.  This process reads KWS log lines from stdin, owns the STM32 SPI
link, runs the PI/PID heater loop at 1 Hz, and exposes the focused local
waveform console on port 80 (or 8080 if 80 is busy).

The web page is intentionally not the old preset/effect panel.  It shows:
  * the lamp duty waveform and keyword-recognition state;
  * the heater temperature/duty waveform, PID terms, ambient/anchor context
    and target band.
"""
from __future__ import annotations

import argparse
import os
import re
import select
import signal
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
APP_DIR = TEST_DIR.parent / "app"
for _path in (TEST_DIR, APP_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import stm32_protocol as p  # noqa: E402
from spi4_bootstrap import (  # noqa: E402
    configure_spi4_pins,
    ensure_spi4_device,
)
from spi_link import Stm32SpiLink, SpiTransactionError  # noqa: E402
from thermal_control import ThermalController  # noqa: E402
from web_control import LampController  # noqa: E402
from console_web import create_console_app  # noqa: E402

try:
    from sg2002_temp import sample as read_temperature  # noqa: E402
except Exception:  # pragma: no cover - optional on a bare host
    read_temperature = None

TRIGGER_RE = re.compile(r"TRIGGER\s+(\w+).*?p=([0-9.]+)")
SCORE_RE = re.compile(
    r"KWS scores hello=([-0-9.]+)\s+welcome=([-0-9.]+)")
WEB_DIR = TEST_DIR / "web"


def available_port(preferred):
    ports = [preferred]
    if preferred != 8080:
        ports.append(8080)
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError("ports 80 and 8080 are both in use")


def start_web(state_fn, history_fn, lamp, thermal, preferred):
    from werkzeug.serving import make_server

    port = available_port(preferred)
    app = create_console_app(
        state_fn, history_fn, lamp, thermal, WEB_DIR, port)
    server = make_server("0.0.0.0", port, app, threaded=True)

    def serve():
        server.serve_forever()

    threading.Thread(target=serve, name="web", daemon=True).start()
    print(f"WEB http://0.0.0.0:{port}/", flush=True)
    return server, port


def main():
    parser = argparse.ArgumentParser(
        description="STM32 lamp + thermal console service")
    parser.add_argument("--hold", type=float, default=1.2)
    parser.add_argument("--mask", type=lambda x: int(x, 0), default=4)
    parser.add_argument("--frequency", type=int, default=100_000)
    parser.add_argument("--web-port", type=int, default=80)
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--temperature-source", choices=("auto", "sysfs", "reg"),
                        default="auto")
    args = parser.parse_args()

    ensure_spi4_device()
    configure_spi4_pins()
    link = Stm32SpiLink(
        bus=4, device=0, frequency=args.frequency,
        retries=5, retry_delay=0.1, settle_time=0.5,
    )
    controller = LampController(link, hold_seconds=args.hold)
    thermal = ThermalController(link)

    lamp_history = deque(maxlen=240)       # 12 s at 20 Hz
    thermal_history_max = 300
    kws = {
        "state": "starting",
        "seen": False,
        "last_keyword": None,
        "last_probability": None,
        "last_trigger_at": None,
        "trigger_count": 0,
        "scores": {"hello": 0.0, "welcome": 0.0},
        "message": "",
    }

    def lamp_snapshot():
        snap = controller.snapshot()
        return {
            "mode": snap["mode"],
            "effect": snap["effect"],
            "brightness": snap["brightness"],
            "target": snap["target"],
            "duties": list(snap["duties"]),
            "active_mask": snap["active_mask"],
            "glyph_state": list(snap["glyph_state"]),
            "link_state": snap["link_state"],
            "fault_flags": snap["fault_flags"],
            "last_event_seq": snap["last_event_seq"],
            "last_control_seq": snap["last_control_seq"],
            "direct_ready": snap["direct_ready"],
            "message": snap["message"],
        }

    def state_fn():
        now = time.monotonic()
        snap = controller.snapshot()
        if not kws["seen"]:
            kws["state"] = "unavailable"
        elif snap["mode"] != "kws":
            kws["state"] = "paused"
        elif controller.gate_until and now < controller.gate_until:
            kws["state"] = "trigger"
        else:
            kws["state"] = "listening"
        return {
            "schema": 2,
            "timestamp": time.time(),
            "link": {
                "state": snap["link_state"],
                "fault_flags": snap["fault_flags"],
                "caps": snap.get("caps", {}),
                "last_control_seq": snap["last_control_seq"],
            },
            "lamp": lamp_snapshot(),
            "kws": dict(kws),
            "thermal": thermal.snapshot(),
        }

    def history_fn():
        return {
            "lamp": list(lamp_history),
            "thermal": list(thermal.history)[-thermal_history_max:],
        }

    web_server = None
    web_port = None
    if not args.no_web:
        web_server, web_port = start_web(
            state_fn, history_fn, controller, thermal, args.web_port)

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    stdin_fd = sys.stdin.fileno()
    buffered = b""
    last_log = 0.0
    last_temp = 0.0
    last_lamp_sample = 0.0
    last_link_state = None
    temp_seq = 0

    try:
        deadline = time.monotonic() + 10.0
        while not stop and time.monotonic() < deadline:
            status = link.transaction()
            controller.tick(status)
            print(
                f"startup link={status.link_state} "
                f"faults=0x{status.fault_flags:04X}",
                flush=True,
            )
            if status.link_state == p.LINK_ACTIVE:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("STM32 link did not become ACTIVE")

        thermal.ensure_link_config()
        print(
            "ready "
            f"web={'off' if args.no_web else web_port} "
            f"direct={'yes' if controller.snapshot()['direct_ready'] else 'no'}",
            flush=True,
        )

        while not stop:
            readable, _, _ = select.select([stdin_fd], [], [], 0.02)
            if readable:
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    stop = True
                else:
                    buffered += chunk
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        text = line.decode("utf-8", "replace")
                        if "KWS" in text or "TRIGGER" in text:
                            kws["seen"] = True
                        match = TRIGGER_RE.search(text)
                        if match:
                            kws["state"] = "trigger"
                            kws["last_keyword"] = match.group(1)
                            kws["last_probability"] = float(match.group(2))
                            kws["last_trigger_at"] = time.time()
                            kws["trigger_count"] += 1
                            controller.on_kws_trigger()
                            print("KWS TRIGGER", flush=True)
                        score = SCORE_RE.search(text)
                        if score:
                            kws["scores"]["hello"] = float(score.group(1))
                            kws["scores"]["welcome"] = float(score.group(2))

            now = time.monotonic()
            if read_temperature is not None and now - last_temp >= 1.0:
                last_temp = now
                try:
                    item = read_temperature(args.temperature_source)
                    temp_seq = (temp_seq + 1) & 0xFFFF
                    link.set_temp(
                        item["temp_mc"], temp_seq, 0, True,
                        0 if item.get("source") == "sysfs" else 1)
                    thermal.sample(
                        item["temp_c"], item["temp_mc"],
                        0 if item.get("source") == "sysfs" else 1,
                        now=now)
                except Exception as exc:  # noqa: BLE001
                    thermal.sample_failed(now)
                    link.set_temp(0, temp_seq, 0, False, 0xFF)
                    print(f"temperature failed: {exc}", flush=True)

            try:
                status = link.transaction()
            except SpiTransactionError as exc:
                print(f"transaction failed: {exc}", flush=True)
                time.sleep(0.05)
                continue

            controller.tick(status)
            if status.link_state == p.LINK_ACTIVE:
                thermal.ensure_link_config()
            elif last_link_state == p.LINK_ACTIVE:
                thermal.link_lost()
            last_link_state = status.link_state

            now = time.monotonic()
            if now - last_lamp_sample >= 0.05:
                last_lamp_sample = now
                lamp_state = controller.snapshot()
                lamp_history.append({
                    "t": round(now, 3),
                    "duties": list(lamp_state["duties"]),
                    "active_mask": lamp_state["active_mask"],
                    "gate": bool(controller.gate_until and
                                 now < controller.gate_until),
                })

            if now - last_log >= 2.0:
                last_log = now
                snap = controller.snapshot()
                t = thermal.snapshot()
                print(
                    f"link={snap['link_state']} mode={snap['mode']} "
                    f"active=0x{snap['active_mask']:X} "
                    f"lamp={snap['brightness']}% "
                    f"temp={t['temp_c']}C heater={t['duty']} "
                    f"enabled={t['enabled']} faults=0x{snap['fault_flags']:04X}",
                    flush=True,
                )
            time.sleep(0.02)
    finally:
        try:
            link.set_heater(0, False)
            link.queue_all_off()
            for _ in range(3):
                link.transaction()
                time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            print(f"ALL_OFF cleanup failed: {exc}", flush=True)
        finally:
            link.close()
            if web_server is not None:
                web_server.shutdown()
            print("stopped", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
