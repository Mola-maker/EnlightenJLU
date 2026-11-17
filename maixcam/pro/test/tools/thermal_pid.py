#!/usr/bin/env python3
"""Command-line PI/PID thermal controller for the STM32 heater.

The SG2002 junction temperature is sampled once per second, the PID is
computed in Python for easy logging/tuning, and the requested HEAT1/HEAT2
duty is sent over the existing SPI4 link.  The STM32 enforces the hard safety
envelope: configured maximum duty, stale/invalid temperature, over-temperature
and link timeout.

The controller is deliberately PI by default.  Derivative action is available
with --kd but is disabled by default: the SG2002 thermal sensor updates only
about once per second and the heater/sensor path has a long lag, so a raw D
term mostly amplifies quantization noise.  Integral action is needed to remove
the steady-state error against the -4 C ambient; conditional integration and
an output clamp provide anti-windup.

Examples:
    # Dry run: print P/I/D every second, never energize HEAT
    python3 -u tools/thermal_pid.py --setpoint 43 --margin 3

    # Active run, 30% hardware limit, 1 Hz
    python3 -u tools/thermal_pid.py --setpoint 43 --margin 3 \
        --enable --max-duty 300

    # More aggressive tuning: Kp=80 permille/C, Ki=1.5 permille/(C*s)
    python3 -u tools/thermal_pid.py --kp 80 --ki 1.5 --enable
"""
from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
APP_DIR = TEST_DIR.parent / "app"
for _path in (TEST_DIR, APP_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import stm32_protocol as p  # noqa: E402
from sg2002_temp import sample as read_temperature  # noqa: E402
from spi4_bootstrap import configure_spi4_pins, ensure_spi4_device  # noqa: E402
from spi_link import SpiTransactionError, Stm32SpiLink  # noqa: E402


def clamp(value, low, high):
    return max(low, min(high, value))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="MaixCAM SG2002 -> STM32 HEAT PI/PID controller")
    parser.add_argument("--setpoint", type=float, default=43.0,
                        help="target junction temperature in C (default 43)")
    parser.add_argument("--margin", type=float, default=3.0,
                        help="hard over-temperature margin in C (default 3)")
    parser.add_argument("--kp", type=float, default=60.0,
                        help="P gain in permille/C (default 60)")
    parser.add_argument("--ki", type=float, default=1.0,
                        help="I gain in permille/(C*s), default 1.0")
    parser.add_argument("--kd", type=float, default=0.0,
                        help="optional D gain, default 0 (noise-sensitive)")
    parser.add_argument("--max-duty", type=int, default=300,
                        help="HEAT PWM limit 0..1000 permille (default 300 = 30%%)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="control period in seconds (default 1.0)")
    parser.add_argument("--count", type=int, default=0,
                        help="number of control steps, 0 = until Ctrl-C")
    parser.add_argument("--frequency", type=int, default=100_000,
                        help="SPI4 clock in Hz (default 100000)")
    parser.add_argument("--source", choices=("auto", "sysfs", "reg"),
                        default="auto", help="temperature source (default auto)")
    parser.add_argument("--enable", action="store_true",
                        help="actually energize HEAT1/HEAT2; default is dry-run")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.count < 0:
        parser.error("--count must be >= 0")
    if not 0 <= args.max_duty <= 1000:
        parser.error("--max-duty must be 0..1000")
    if args.margin < 0:
        parser.error("--margin must be >= 0")
    return args


def make_link(args):
    ensure_spi4_device()
    configure_spi4_pins()
    return Stm32SpiLink(
        bus=4,
        device=0,
        frequency=args.frequency,
        retries=8,
        retry_delay=0.02,
        settle_time=0.5,
    )


def wait_active(link, timeout=10.0):
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = link.transaction()
        if status.link_state == p.LINK_ACTIVE:
            return status
        time.sleep(0.1)
    return status


def decode_heater_flags(flags):
    names = []
    if flags & 0x01:
        names.append("enabled")
    if flags & 0x02:
        names.append("active")
    if flags & 0x04:
        names.append("overtemp")
    if flags & 0x08:
        names.append("temp-invalid")
    if flags & 0x10:
        names.append("configured")
    return "+".join(names) if names else "off"


def start_keepalive(link, stop_event, log_state):
    """Keep the bit-banged SPI4 link serviced even while PID sleeps 1 s.

    The software SPI driver occasionally returns a half frame.  A 20 ms
    keepalive loop with retries keeps the STM32's 500 ms link watchdog fed and
    makes the 1 Hz temperature/PID cadence independent of SPI jitter.
    """
    def worker():
        last_error = 0.0
        while not stop_event.is_set():
            try:
                status = link.transaction()
                log_state["link_state"] = status.link_state
                log_state["status"] = status
            except Exception as exc:  # noqa: BLE001
                now = time.monotonic()
                if now - last_error >= 2.0:
                    print(f"SPI keepalive: {exc}", file=sys.stderr, flush=True)
                    last_error = now
            time.sleep(0.02)

    thread = threading.Thread(target=worker, name="spi-keepalive", daemon=True)
    thread.start()
    return thread


def main(argv=None):
    args = parse_args(argv)
    setpoint_mc = int(round(args.setpoint * 1000))
    margin_mc = int(round(args.margin * 1000))
    kp_x100 = int(round(args.kp * 100))
    ki_x100 = int(round(args.ki * 100))
    kd_x100 = int(round(args.kd * 100))
    max_duty = args.max_duty

    link = None
    stop = False
    integral = 0.0
    previous_temp_c = None
    previous_tick = None
    temp_seq = 0
    overtemp = False
    last_link_state = None
    last_config_at = 0.0

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        link = make_link(args)
        link.set_thermal_config(
            setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100,
            max_duty, args.enable)
        status = wait_active(link)
        if status is None or status.link_state != p.LINK_ACTIVE:
            raise RuntimeError(
                f"STM32 did not become ACTIVE: "
                f"{getattr(status, 'link_state', 'no status')}")

        keepalive_stop = threading.Event()
        keepalive_state = {"link_state": status.link_state, "status": status}
        keepalive_thread = start_keepalive(link, keepalive_stop, keepalive_state)
        last_link_state = status.link_state

        print(
            f"READY link=ACTIVE setpoint={args.setpoint:.2f}C "
            f"margin=±{args.margin:.2f}C "
            f"Kp={args.kp:.1f}permille/C Ki={args.ki:.2f}permille/(C*s) "
            f"Kd={args.kd:.2f} max={max_duty}permille "
            f"heater={'ON' if args.enable else 'DRY-RUN'}",
            flush=True)

        steps = 0
        while not stop and (args.count == 0 or steps < args.count):
            started = time.monotonic()
            timestamp = time.strftime("%H:%M:%S")
            try:
                item = read_temperature(args.source)
                temp_c = float(item["temp_c"])
                temp_mc = int(item["temp_mc"])
                source = 0 if item.get("source") == "sysfs" else 1
                temp_seq = (temp_seq + 1) & 0xFFFF
                link.set_temp(temp_mc, temp_seq, 0, True, source)
            except Exception as exc:  # noqa: BLE001
                temp_c = None
                temp_mc = 0
                error_c = 0.0
                p_term = i_term = d_term = 0.0
                output = 0.0
                link.set_temp(0, temp_seq, 0, False, 0xFF)
                print(f"{timestamp} temp read failed: {exc}",
                      file=sys.stderr, flush=True)

            now = time.monotonic()
            if temp_c is not None:
                error_mc = setpoint_mc - temp_mc
                error_c = error_mc / 1000.0
                p_term = args.kp * error_c

                if args.kd > 0.0 and previous_temp_c is not None:
                    dt = max(0.05, now - previous_tick)
                    d_term = -args.kd * (temp_c - previous_temp_c) / dt
                else:
                    d_term = 0.0

                dt = max(0.05, now - previous_tick) if previous_tick else args.interval
                candidate = integral + args.ki * error_c * dt
                candidate = clamp(candidate, 0.0, float(max_duty))

                # Conditional integration: do not wind farther into a
                # saturated output.  The P term handles fast approach.
                unsaturated = p_term + candidate + d_term
                if (unsaturated > max_duty and error_c > 0.0) or (
                        unsaturated < 0.0 and error_c < 0.0):
                    candidate = integral
                integral = clamp(candidate, 0.0, float(max_duty))
                i_term = integral
                output = clamp(p_term + i_term + d_term, 0.0, float(max_duty))

                upper_c = args.setpoint + args.margin
                lower_release_c = upper_c - 1.0
                if temp_c >= upper_c:
                    overtemp = True
                    output = 0.0
                    integral = 0.0
                    i_term = 0.0
                elif overtemp and temp_c <= lower_release_c:
                    overtemp = False

                if not args.enable:
                    output = 0.0
                link.set_heater(int(round(output)), args.enable)
                previous_temp_c = temp_c
                previous_tick = now
            else:
                error_c = 0.0
                link.set_heater(0, False)

            # The keepalive thread owns SPI transactions.  Wait long enough
            # for the latest temperature/heater request to be sent and for a
            # fresh STATUS frame to come back.
            time.sleep(0.06)
            status = keepalive_state.get("status")
            link_state = getattr(status, "link_state", -1)
            actual_duty = getattr(status, "heater_duty", 0)
            heater_flags = getattr(status, "heater_flags", 0)
            temp_age = getattr(status, "temp_age_ms", 0)
            if link_state != p.LINK_ACTIVE:
                actual_duty = 0
                heater_flags = 0

            # THERMAL_CONFIG is sent once by the link layer.  Resend it after
            # a link/session recovery or every 10 s so a reset STM32 does not
            # keep an old safety envelope.
            loop_now = time.monotonic()
            if link_state == p.LINK_ACTIVE and (
                    link_state != last_link_state or
                    loop_now - last_config_at >= 10.0):
                link.set_thermal_config(
                    setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100,
                    max_duty, args.enable)
                last_config_at = loop_now
            last_link_state = link_state

            temp_text = "----.--" if temp_c is None else f"{temp_c:7.3f}"
            state_text = "OVERTEMP" if overtemp else (
                "DRY-RUN" if not args.enable else "RUN")
            print(
                f"{timestamp} T={temp_text}C sp={args.setpoint:6.2f}C "
                f"err={error_c:+6.2f}C P={p_term:7.2f} I={i_term:7.2f} "
                f"D={d_term:+6.2f} req={output:6.1f} "
                f"heat={actual_duty:4d} flags=0x{heater_flags:02X} "
                f"[{decode_heater_flags(heater_flags)}] "
                f"age={temp_age}ms link={link_state} {state_text}",
                flush=True)

            steps += 1
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"thermal_pid: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if "keepalive_stop" in locals():
            keepalive_stop.set()
        if "keepalive_thread" in locals():
            keepalive_thread.join(1.0)
        if link is not None:
            try:
                link.set_heater(0, False)
                link.set_thermal_config(
                    setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100,
                    max_duty, False)
                link.queue_all_off()
                for _ in range(3):
                    link.transaction()
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup failed: {exc}", file=sys.stderr, flush=True)
            finally:
                link.close()
        print("stopped; HEAT requested OFF", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
