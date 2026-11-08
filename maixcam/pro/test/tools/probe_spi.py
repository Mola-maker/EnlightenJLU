#!/usr/bin/env python3
"""Probe the STM32 SPI v1 link on MaixCAM Pro.

The external A22-A25 header is software SPI4 (/dev/spidev4.0), not the
hardware SPI3 controller (/dev/spidev3.0).  This probe configures the
pins, binds spi4@gpio if necessary, and retries HELLO until a valid
STATUS frame is received.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

TEST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

import spidev  # noqa: E402

import stm32_protocol as p  # noqa: E402
from spi4_bootstrap import (  # noqa: E402
    configure_spi4_pins,
    ensure_spi4_device,
    parse_spi4_response,
)

FAULT_NAMES = {
    0: "CRC",
    1: "FORMAT",
    2: "SPI",
    3: "UNKNOWN_TLV",
    4: "NEED_HELLO",
    5: "TEMP_STALE",
    6: "TEMP_INVALID",
    7: "HEATER_UNSUPPORTED",
    8: "BAD_MASK",
    9: "PROTOCOL",
    10: "LINK_LOST",
}


def fault_names(flags):
    return [name for bit, name in FAULT_NAMES.items()
            if flags & (1 << bit)]


def main():
    parser = argparse.ArgumentParser(description="Probe STM32 SPI v1")
    parser.add_argument("bus", nargs="?", type=int, default=4,
                        help="spidev bus (default: 4 for software SPI4)")
    parser.add_argument("device", nargs="?", type=int, default=0)
    parser.add_argument("--frequency", type=int, default=500_000)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--settle", type=float, default=0.5,
                        help="seconds to wait after opening software SPI4")
    args = parser.parse_args()

    if args.bus == 4:
        ensure_spi4_device()
        configure_spi4_pins()

    spi = spidev.SpiDev()
    spi.open(args.bus, args.device)
    spi.bits_per_word = 8
    spi.max_speed_hz = args.frequency
    if args.bus == 4 and args.settle > 0:
        time.sleep(args.settle)
    # Leave mode/cshigh at the driver default for software SPI4.

    session = 0x12345678
    good = 0
    try:
        for attempt in range(max(1, args.attempts)):
            tx = p.build_frame(
                p.TYPE_CONTROL,
                1 + attempt,
                session,
                p.hello(heartbeat_ms=50,
                        features=(p.FEATURE_PERFORM | p.FEATURE_TEMP |
                                  p.FEATURE_HEARTBEAT)),
            )
            rx = bytes(spi.xfer2(list(tx)))
            try:
                status, caps = parse_spi4_response(rx)
            except Exception as exc:  # noqa: BLE001
                print(f"attempt {attempt}: invalid response "
                      f"{rx[:8].hex()}: {exc}")
                time.sleep(0.05)
                continue

            good += 1
            print(
                f"attempt {attempt}: link={status.link_state} "
                f"ctl={status.last_control_seq} "
                f"faults=0x{status.fault_flags:04X} "
                f"({','.join(fault_names(status.fault_flags)) or 'none'}) "
                f"active=0x{status.active_mask:X} "
                f"temp={status.temp_mc}mC age={status.temp_age_ms}ms "
                f"valid={status.temp_valid}")
            print(f"  glyphs={status.glyph_state} caps={caps}")
            time.sleep(0.05)
            if status.link_state == p.LINK_ACTIVE:
                break
    finally:
        spi.close()

    print(f"valid STATUS frames: {good}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
