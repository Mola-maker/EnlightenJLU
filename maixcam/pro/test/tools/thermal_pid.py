#!/usr/bin/env python3
"""Command-line PI/PID thermal controller for the STM32 heater.

The SG2002 junction temperature is sampled once per second, the PI is
computed in Python for easy logging/tuning, and the requested HEAT1/HEAT2
duty is sent over the existing SPI4 link.  The STM32 enforces the hard safety
envelope: configured maximum duty, stale/invalid temperature, over-temperature
and link timeout.

The controller is deliberately PI by default.  Derivative action is available
with --kd but is disabled by default: the SG2002 thermal sensor updates only
about once per second and the heater/sensor path has a long lag, so a raw D
term mostly amplifies quantization noise.

A plain PI would integrate while the output is saturated at max_duty during
the initial heat-up and then overshoot by several degrees.  This command
therefore uses the warm-start controller in ``thermal_pi.py``:

* below ``setpoint - warmup_band`` the output is open-loop ``max_duty`` and
  the integrator is held at ``hold_duty``;
* inside the band the PI starts from ``hold_duty`` instead of the accumulated
  saturation value;
* integral action is frozen whenever the total output is already saturated.

The SPI link also self-heals: HELLO and THERMAL_CONFIG are sent in the same
frame after a reset/timeout, and the transport is reopened after repeated bad
frames.  The log prints STM32 uptime and reset count so a supply brown-out or
MCU reset can be distinguished from a control-loop problem.

Examples:
    # Dry run: print P/I/D every second, never energize HEAT
    python3 -u tools/thermal_pid.py --setpoint 43 --margin 3

    # Active run: 60% hardware ceiling, 35% holding feed-forward
    python3 -u tools/thermal_pid.py --setpoint 43 --margin 3 \
        --enable --max-duty 600 --hold-duty 350

    # More aggressive tuning
    python3 -u tools/thermal_pid.py --kp 80 --ki 1.5 --enable
"""
from __future__ import annotations

import argparse
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
from spi_link import Stm32SpiLink  # noqa: E402
from thermal_pi import ThermalPI  # noqa: E402


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
    parser.add_argument("--max-duty", type=int, default=600,
                        help="HEAT PWM limit 0..1000 permille (default 600)")
    parser.add_argument("--hold-duty", type=int, default=None,
                        help="PI starting/holding duty near the setpoint "
                             "(default 350, clipped to --max-duty)")
    parser.add_argument("--warmup-band", type=float, default=2.0,
                        help="distance below setpoint at which open-loop "
                             "warm-up hands over to PI (default 2 C)")
    parser.add_argument("--warmup-hysteresis", type=float, default=0.5,
                        help="extra distance before warm-up re-arms after a "
                             "drop (default 0.5 C)")
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
    if args.hold_duty is None:
        args.hold_duty = min(350, args.max_duty)
    elif not 0 <= args.hold_duty <= 1000:
        parser.error("--hold-duty must be 0..1000")
    elif args.hold_duty > args.max_duty:
        parser.error("--hold-duty must be <= --max-duty")
    if args.warmup_band < 0:
        parser.error("--warmup-band must be >= 0")
    if args.warmup_hysteresis < 0:
        parser.error("--warmup-hysteresis must be >= 0")
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


def start_keepalive(link, stop_event, log_state,
                    reopen_after=2, reopen_interval=5.0):
    """Keep the bit-banged SPI4 link serviced even while PID sleeps 1 s.

    The software SPI driver occasionally returns a half frame.  A 20 ms
    keepalive loop with retries keeps the STM32's 500 ms link watchdog fed and
    makes the 1 Hz temperature/PID cadence independent of SPI jitter.

    A failed transaction is a real link failure after the transport's own
    eight retries, so the shared state is marked down instead of silently
    keeping an old ACTIVE/heat=599 status on screen.  Repeated failures
    reopen /dev/spidev4.0, which recovers a wedged spi-gpio descriptor.
    """
    def worker():
        last_error = 0.0
        last_reopen = 0.0
        failures = 0
        while not stop_event.is_set():
            now = time.monotonic()
            try:
                status = link.transaction()
                failures = 0
                log_state.update(
                    status=status,
                    ok_at=time.monotonic(),
                    link_state=status.link_state,
                    failures=0,
                )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                now = time.monotonic()
                # Do not leave a stale ACTIVE/heat=599 snapshot visible.
                log_state.update(
                    status=None,
                    ok_at=0.0,
                    link_state=-1,
                    failures=failures,
                )
                if now - last_error >= 2.0:
                    print(f"SPI keepalive: {exc} (failures={failures})",
                          file=sys.stderr, flush=True)
                    last_error = now
                if (failures >= reopen_after and
                        now - last_reopen >= reopen_interval):
                    last_reopen = now
                    try:
                        link.reopen()
                        print("SPI keepalive: transport reopened",
                              file=sys.stderr, flush=True)
                    except Exception as reopen_exc:  # noqa: BLE001
                        print(f"SPI keepalive: reopen failed: {reopen_exc}",
                              file=sys.stderr, flush=True)
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
    hold_duty = args.hold_duty

    controller = ThermalPI(
        setpoint_c=args.setpoint,
        margin_c=args.margin,
        kp=args.kp,
        ki=args.ki,
        kd=args.kd,
        max_duty=max_duty,
        hold_duty=hold_duty,
        warmup_band_c=args.warmup_band,
        warmup_hysteresis_c=args.warmup_hysteresis,
    )

    link = None
    stop = False
    temp_seq = 0
    last_config_at = 0.0
    last_config_request_at = 0.0
    not_following_since = None
    last_not_following_warn = 0.0
    previous_stm32_uptime_ms = None
    stm32_resets = 0

    def set_thermal_config(now, enabled):
        nonlocal last_config_at
        link.set_thermal_config(
            setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100,
            max_duty, enabled)
        last_config_at = now

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        link = make_link(args)
        set_thermal_config(time.monotonic(), args.enable)
        status = wait_active(link)
        if status is None or status.link_state != p.LINK_ACTIVE:
            raise RuntimeError(
                f"STM32 did not become ACTIVE: "
                f"{getattr(status, 'link_state', 'no status')}")

        keepalive_stop = threading.Event()
        keepalive_state = {
            "status": status,
            "ok_at": time.monotonic(),
            "link_state": status.link_state,
            "failures": 0,
        }
        keepalive_thread = start_keepalive(link, keepalive_stop, keepalive_state)

        print(
            f"READY link=ACTIVE setpoint={args.setpoint:.2f}C "
            f"margin=±{args.margin:.2f}C "
            f"Kp={args.kp:.1f}permille/C Ki={args.ki:.2f}permille/(C*s) "
            f"Kd={args.kd:.2f} max={max_duty}permille "
            f"hold={hold_duty}permille warmup={args.warmup_band:.2f}C "
            f"heater={'ON' if args.enable else 'DRY-RUN'}",
            flush=True)

        steps = 0
        while not stop and (args.count == 0 or steps < args.count):
            started = time.monotonic()
            timestamp = time.strftime("%H:%M:%S")
            output = 0.0
            requested_duty = 0

            try:
                item = read_temperature(args.source)
                temp_c = float(item["temp_c"])
                temp_mc = int(item["temp_mc"])
                source = 0 if item.get("source") == "sysfs" else 1
                temp_seq = (temp_seq + 1) & 0xFFFF
                link.set_temp(temp_mc, temp_seq, 0, True, source)

                pi = controller.update(temp_c, time.monotonic())
                error_c = pi.error_c
                p_term = pi.p_term
                i_term = pi.i_term
                d_term = pi.d_term
                output = pi.output
                requested_duty = int(round(output)) if args.enable else 0
                link.set_heater(requested_duty, args.enable)
            except Exception as exc:  # noqa: BLE001
                temp_c = None
                error_c = p_term = i_term = d_term = output = 0.0
                requested_duty = 0
                controller.suspend()
                link.set_temp(0, temp_seq, 0, False, 0xFF)
                link.set_heater(0, False)
                print(f"{timestamp} temp read failed: {exc}",
                      file=sys.stderr, flush=True)

            # The keepalive thread owns SPI transactions.  Wait long enough
            # for the latest temperature/heater request to be sent and for a
            # fresh STATUS frame to come back.
            time.sleep(0.06)
            loop_now = time.monotonic()
            status = keepalive_state.get("status")
            status_ok = (status is not None and
                         keepalive_state.get("ok_at", 0.0) and
                         loop_now - keepalive_state["ok_at"] < 1.0)
            if status_ok:
                link_state = getattr(status, "link_state", -1)
                actual_duty = getattr(status, "heater_duty", 0)
                heater_flags = getattr(status, "heater_flags", 0)
                temp_age = getattr(status, "temp_age_ms", 0)
                fault_flags = getattr(status, "fault_flags", 0)
                stm32_uptime_ms = getattr(status, "stm32_uptime_ms", None)
            else:
                link_state = -1
                actual_duty = 0
                heater_flags = 0
                temp_age = 0
                fault_flags = 0
                stm32_uptime_ms = None

            if link_state != p.LINK_ACTIVE:
                actual_duty = 0

            if stm32_uptime_ms is not None:
                if (previous_stm32_uptime_ms is not None and
                        stm32_uptime_ms + 2000 < previous_stm32_uptime_ms):
                    stm32_resets += 1
                    print(
                        f"STM32 reset detected: uptime "
                        f"{previous_stm32_uptime_ms}ms -> "
                        f"{stm32_uptime_ms}ms (count={stm32_resets})",
                        file=sys.stderr, flush=True)
                    # The firmware cleared its thermal envelope on reset.
                    set_thermal_config(loop_now, args.enable)
                previous_stm32_uptime_ms = stm32_uptime_ms

            # THERMAL_CONFIG is normally re-armed inside Stm32SpiLink when
            # STATUS loses ACTIVE/configured.  Keep an independent retry in
            # the main loop as a backstop, and retain the 10 s refresh so a
            # silently reset STM32 cannot keep an old envelope.
            if status_ok and link_state == p.LINK_ACTIVE:
                configured = bool(heater_flags & 0x10)
                enabled = bool(heater_flags & 0x01)
                need_config = (not configured or
                               (args.enable and not enabled))
                if need_config and loop_now - last_config_request_at >= 0.5:
                    set_thermal_config(loop_now, args.enable)
                    last_config_request_at = loop_now
                elif loop_now - last_config_at >= 10.0:
                    set_thermal_config(loop_now, args.enable)

            # Detect a configured, enabled actuator that is still applying
            # zero output.  This is the signature of a lost HEATER frame or a
            # firmware-side safety cut-off that the host did not request.
            if (status_ok and link_state == p.LINK_ACTIVE and args.enable and
                    requested_duty > 50 and actual_duty == 0 and
                    (heater_flags & 0x10) and (heater_flags & 0x01) and
                    not (heater_flags & (0x04 | 0x08))):
                if not_following_since is None:
                    not_following_since = loop_now
                elif (loop_now - not_following_since >= 2.0 and
                      loop_now - last_not_following_warn >= 5.0):
                    print(
                        "warning: HEAT request is not reaching the actuator "
                        f"(req={requested_duty} flags=0x{heater_flags:02X})",
                        file=sys.stderr, flush=True)
                    last_not_following_warn = loop_now
            else:
                not_following_since = None

            temp_text = "----.--" if temp_c is None else f"{temp_c:7.3f}"
            if controller.last.overtemp:
                state_text = "OVERTEMP"
            elif not args.enable:
                state_text = "DRY-RUN"
            elif controller.last.warmup:
                state_text = "WARMUP"
            else:
                state_text = "RUN"
            if status_ok and link_state != p.LINK_ACTIVE:
                state_text += f"/LINK-{link_state}"
            render_link = "DOWN" if not status_ok else str(link_state)
            render_up = ("?" if stm32_uptime_ms is None
                         else f"{stm32_uptime_ms / 1000.0:.0f}")
            print(
                f"{timestamp} T={temp_text}C sp={args.setpoint:6.2f}C "
                f"err={error_c:+6.2f}C P={p_term:7.2f} I={i_term:7.2f} "
                f"D={d_term:+6.2f} req={requested_duty:4d} "
                f"heat={actual_duty:4d} flags=0x{heater_flags:02X} "
                f"[{decode_heater_flags(heater_flags)}] "
                f"age={temp_age}ms fault=0x{fault_flags:04X} "
                f"link={render_link} up={render_up}s rst={stm32_resets} "
                f"{state_text}",
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
                set_thermal_config(time.monotonic(), False)
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
