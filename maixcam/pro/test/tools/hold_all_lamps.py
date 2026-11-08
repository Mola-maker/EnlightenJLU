#!/usr/bin/env python3
"""Hold the STM32 test firmware's 13 lamp PWM outputs active.

GLYPH_ALL_LAMPS is bit 2 (mask 0x4).  The STM32 performance engine maps it
to PWM1..PWM13 and never to HEAT1/HEAT2.  GATE drives those outputs to
100% and leaves them in HOLD; the loop keeps a 20 Hz heartbeat alive so the
STM32 does not enter its 500 ms link-lost shutdown.

Stop with SIGTERM/SIGINT; the handler sends ALL_OFF before closing.
"""
from __future__ import annotations

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

ALL_LAMPS_MASK = 1 << 2
LAMP_GLYPH = 2


def seq_ge(value: int, reference: int) -> bool:
    return ((value - reference) & 0xFFFFFFFF) < 0x80000000


def main() -> int:
    ensure_spi4_device()
    configure_spi4_pins()
    link = Stm32SpiLink(
        bus=4,
        device=0,
        frequency=500_000,
        retries=5,
        retry_delay=0.1,
        settle_time=0.5,
    )

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

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

        gate_event = link.queue_perform(ALL_LAMPS_MASK, p.ACTION_GATE)
        gate_acked = False
        was_active = True
        last_log = 0.0

        while not stop:
            try:
                status = link.transaction()
            except SpiTransactionError as exc:
                print(f"transaction failed: {exc}", flush=True)
                time.sleep(0.05)
                continue

            active = status.link_state == p.LINK_ACTIVE
            if not active:
                was_active = False
                gate_acked = False
                gate_event = None
            elif not was_active:
                was_active = True
                gate_event = link.queue_perform(ALL_LAMPS_MASK, p.ACTION_GATE)
                gate_acked = False
            elif gate_event is not None and seq_ge(
                    status.last_event_seq, gate_event):
                gate_acked = True
                gate_event = None

            now = time.monotonic()
            if now - last_log >= 2.0:
                last_log = now
                print(
                    f"link={status.link_state} "
                    f"active_mask=0x{status.active_mask:X} "
                    f"glyphs={status.glyph_state} "
                    f"lamp_state={status.glyph_state[LAMP_GLYPH]} "
                    f"gate_acked={gate_acked} "
                    f"faults=0x{status.fault_flags:04X}",
                    flush=True,
                )
            time.sleep(0.05)
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
