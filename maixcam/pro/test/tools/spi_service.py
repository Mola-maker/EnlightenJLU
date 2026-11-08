#!/usr/bin/env python3
"""SPI owner for the 13-lamp test.

Reads KWS log lines from stdin and owns the STM32 SPI link in a separate
process so the bit-banged SPI4 bus is not starved by the KWS model.  It keeps
PWM1..PWM13 in STANDBY and applies GATE/RELEASE when the KWS emits a trigger.
HEAT1/HEAT2 are never included in the mask.
"""
from __future__ import annotations

import argparse
import os
import re
import select
import signal
import sys
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

TRIGGER_RE = re.compile(r"GATE mask=0x([0-9A-Fa-f]+)")


def seq_ge(value, reference):
    return ((value - reference) & 0xFFFFFFFF) < 0x80000000


def main():
    parser = argparse.ArgumentParser(description="STM32 13-lamp SPI service")
    parser.add_argument("--hold", type=float, default=1.2)
    parser.add_argument("--mask", type=lambda x: int(x, 0), default=4)
    parser.add_argument("--frequency", type=int, default=500_000)
    args = parser.parse_args()

    ensure_spi4_device()
    configure_spi4_pins()
    link = Stm32SpiLink(
        bus=4, device=0, frequency=args.frequency,
        retries=5, retry_delay=0.1, settle_time=0.5,
    )

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    standby_sent = False
    gate_until = 0.0
    gate_event = None
    gate_applied = False
    was_active = False
    last_log = 0.0
    buffered = b""
    stdin_fd = sys.stdin.fileno()

    try:
        deadline = time.monotonic() + 10.0
        while not stop and time.monotonic() < deadline:
            status = link.transaction()
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
                        if match and (int(match.group(1), 16) & args.mask):
                            gate_until = time.monotonic() + args.hold
                            gate_event = link.queue_perform(
                                args.mask, p.ACTION_GATE)
                            gate_applied = False
                            # GATE owns the outputs now; suppress the initial
                            # STANDBY request until this performance ends.
                            standby_sent = True
                            print(
                                f"TRIGGER -> GATE mask=0x{args.mask:X} "
                                f"event={gate_event} hold={args.hold}s",
                                flush=True,
                            )

            try:
                status = link.transaction()
            except SpiTransactionError as exc:
                print(f"transaction failed: {exc}", flush=True)
                time.sleep(0.05)
                continue

            now = time.monotonic()
            active = status.link_state == p.LINK_ACTIVE
            if not active:
                # The STM32 has forced all outputs off.  Keep the pending
                # gate deadline, but re-apply it after HELLO recovery.
                was_active = False
                standby_sent = False
                gate_event = None
                gate_applied = False
            else:
                if not was_active:
                    was_active = True
                    standby_sent = False
                    gate_event = None
                    gate_applied = False

                if gate_until and now < gate_until:
                    if not gate_applied and gate_event is None:
                        gate_event = link.queue_perform(
                            args.mask, p.ACTION_GATE)
                        print(
                            f"PERFORM GATE mask=0x{args.mask:X} "
                            f"event={gate_event}",
                            flush=True,
                        )
                    elif gate_event is not None and seq_ge(
                            status.last_event_seq, gate_event):
                        gate_applied = True
                        gate_event = None
                        print(
                            f"GATE ACK active=0x{status.active_mask:X} "
                            f"glyphs={status.glyph_state}",
                            flush=True,
                        )
                elif gate_until:
                    link.queue_perform(args.mask, p.ACTION_RELEASE)
                    print(
                        f"PERFORM RELEASE mask=0x{args.mask:X}",
                        flush=True,
                    )
                    gate_until = 0.0
                    gate_event = None
                    gate_applied = False
                    # RELEASE returns to STANDBY inside the STM32 engine.
                    standby_sent = True
                elif not standby_sent:
                    standby_sent = True
                    link.queue_perform(args.mask, p.ACTION_STANDBY)
                    print("PERFORM STANDBY", flush=True)

            if now - last_log >= 2.0:
                last_log = now
                print(
                    f"link={status.link_state} "
                    f"active=0x{status.active_mask:X} "
                    f"glyphs={status.glyph_state} "
                    f"last_evt={status.last_event_seq} "
                    f"gate_applied={gate_applied} "
                    f"faults=0x{status.fault_flags:04X}",
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
            print("stopped", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
