#!/usr/bin/env python3
"""SPI owner and web control service for the 13-lamp test.

The KWS model runs in a separate process because it can starve the bit-banged
SPI4 bus.  This process reads KWS log lines from stdin, owns the STM32 SPI
link, and serves a small responsive web UI on port 80 (or 8080 if 80 is
busy).  HEAT1/HEAT2 are never included in any commanded lamp mask.
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
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_DIR))

import stm32_protocol as p  # noqa: E402
from spi4_bootstrap import (  # noqa: E402
    configure_spi4_pins,
    ensure_spi4_device,
)
from spi_link import Stm32SpiLink, SpiTransactionError  # noqa: E402
from web_control import LampController, create_app  # noqa: E402

TRIGGER_RE = re.compile(r"GATE mask=0x([0-9A-Fa-f]+)")
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


def start_web(controller, preferred):
    from werkzeug.serving import make_server

    port = available_port(preferred)
    app = create_app(controller, WEB_DIR, port)
    server = make_server("0.0.0.0", port, app, threaded=True)

    def serve():
        server.serve_forever()

    threading.Thread(target=serve, name="web", daemon=True).start()
    print(f"WEB http://0.0.0.0:{port}/", flush=True)
    return server, port


def main():
    parser = argparse.ArgumentParser(description="STM32 13-lamp SPI service")
    parser.add_argument("--hold", type=float, default=1.2)
    parser.add_argument("--mask", type=lambda x: int(x, 0), default=4)
    parser.add_argument("--frequency", type=int, default=500_000)
    parser.add_argument("--web-port", type=int, default=80)
    parser.add_argument("--no-web", action="store_true")
    args = parser.parse_args()

    ensure_spi4_device()
    configure_spi4_pins()
    link = Stm32SpiLink(
        bus=4, device=0, frequency=args.frequency,
        retries=5, retry_delay=0.1, settle_time=0.5,
    )
    controller = LampController(link, hold_seconds=args.hold)
    web_server = None
    if not args.no_web:
        web_server, web_port = start_web(controller, args.web_port)

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    stdin_fd = sys.stdin.fileno()
    buffered = b""
    last_log = 0.0

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
                        match = TRIGGER_RE.search(text)
                        if match:
                            controller.on_kws_trigger()
                            print("KWS TRIGGER", flush=True)

            try:
                status = link.transaction()
            except SpiTransactionError as exc:
                print(f"transaction failed: {exc}", flush=True)
                time.sleep(0.05)
                continue

            controller.tick(status)
            now = time.monotonic()
            if now - last_log >= 2.0:
                last_log = now
                snap = controller.snapshot()
                print(
                    f"link={snap['link_state']} mode={snap['mode']} "
                    f"active=0x{snap['active_mask']:X} "
                    f"glyphs={snap['glyph_state']} "
                    f"web={snap['brightness']}% "
                    f"faults=0x{snap['fault_flags']:04X}",
                    flush=True,
                )
            time.sleep(0.02)
    finally:
        try:
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
