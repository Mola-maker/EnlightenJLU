#!/usr/bin/env python3
"""Open-loop HEAT step-response test for the MaixCAM/STM32 heater.

The normal thermal_pid.py loop mixes three things: controller action, Python
CPU load and SPI traffic.  This diagnostic keeps all of them constant and only
switches HEAT1/HEAT2 on and off.  It is the quickest way to answer:

  * does the SG2002 temperature sensor actually follow the heater?
  * is the measured duty really applied for the full on-phase?
  * does the STM32 reset or lose the SPI link under sustained load?

Run it with the normal S99 service stopped (the SPI bus lock enforces this):

    python3 -u tools/heater_step_test.py --enable --duty 600 \
        --off 60 --on 120 --cycles 2

Output is CSV on stdout.  A copy can be saved on the device for later analysis:

    python3 -u tools/heater_step_test.py --enable > /tmp/heater-step.csv
"""
from __future__ import annotations

import argparse
import signal
import sys
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


def read_cpu_khz():
    for path in (
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq",
    ):
        try:
            return int(Path(path).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
    return None


def read_load1():
    try:
        return float(Path("/proc/loadavg").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError):
        return None


def fmt(value):
    return "" if value is None else value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Open-loop HEAT step response / thermal coupling test")
    parser.add_argument("--duty", type=int, default=600,
                        help="on-phase duty in permille (default 600)")
    parser.add_argument("--off", type=float, default=60.0,
                        help="off phase seconds (default 60)")
    parser.add_argument("--on", type=float, default=120.0,
                        help="on phase seconds (default 120)")
    parser.add_argument("--cycles", type=int, default=2,
                        help="number of off/on pairs (default 2)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="sample/log interval (default 1.0)")
    parser.add_argument("--frequency", type=int, default=100_000,
                        help="SPI4 clock in Hz (default 100000)")
    parser.add_argument("--source", choices=("auto", "sysfs", "reg"),
                        default="auto", help="temperature source")
    parser.add_argument("--setpoint", type=float, default=43.0,
                        help="safety setpoint for the test (default 43 C)")
    parser.add_argument("--margin", type=float, default=3.0,
                        help="safety over-temperature margin (default 3 C)")
    parser.add_argument("--split-heat", action="store_true",
                        help="use the 5V/2A split-channel mode")
    parser.add_argument("--enable", action="store_true",
                        help="required: this test energizes the heater")
    args = parser.parse_args(argv)
    if not args.enable:
        parser.error("--enable is required for the heater step test")
    if not 0 <= args.duty <= 1000:
        parser.error("--duty must be 0..1000")
    if args.off <= 0 or args.on <= 0:
        parser.error("--off/--on must be > 0")
    if args.cycles < 1:
        parser.error("--cycles must be >= 1")
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.setpoint < 0 or args.margin < 0:
        parser.error("--setpoint/--margin must be >= 0")
    return args


def main(argv=None):
    args = parse_args(argv)
    ensure_spi4_device()
    configure_spi4_pins()
    link = Stm32SpiLink(
        bus=4, device=0, frequency=args.frequency,
        retries=8, retry_delay=0.02, settle_time=0.5,
    )

    phases = []
    for _ in range(args.cycles):
        phases.append(("off", args.off))
        phases.append(("on", args.on))
    phases.append(("off", args.off))

    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    temp_seq = 0
    phase_index = 0
    phase_started = time.monotonic()
    started = phase_started
    next_sample = started
    last_status = None
    last_error = 0.0
    phase_samples = {}

    print(
        "t_s,phase,temp_c,req_duty,heat_duty,flags,fault,link,"
        "stm32_up_s,load1,cpu_khz",
        flush=True,
    )
    try:
        link.set_thermal_config(int(round(args.setpoint * 1000)),
                                int(round(args.margin * 1000)),
                                0, 0, 0, max(args.duty, 1), True,
                                args.split_heat)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            last_status = link.transaction()
            if last_status.link_state == p.LINK_ACTIVE:
                break
        else:
            raise RuntimeError("STM32 did not become ACTIVE")

        while not stop and phase_index < len(phases):
            now = time.monotonic()
            phase, duration = phases[phase_index]
            elapsed = now - phase_started
            if elapsed >= duration:
                print(f"# phase {phase} finished after {elapsed:.1f}s",
                      flush=True)
                phase_index += 1
                if phase_index >= len(phases):
                    break
                phase_started = time.monotonic()
                phase, duration = phases[phase_index]
                print(f"# phase {phase} started", flush=True)

            sample_now = False
            if now >= next_sample:
                next_sample = now + args.interval
                sample_now = True
                try:
                    item = read_temperature(args.source)
                    temp_c = item["temp_c"]
                    temp_seq = (temp_seq + 1) & 0xFFFF
                    link.set_temp(item["temp_mc"], temp_seq, 0, True,
                                  0 if item.get("source") == "sysfs" else 1)
                except Exception as exc:  # noqa: BLE001
                    temp_c = None
                    link.set_temp(0, temp_seq, 0, False, 0xFF)
                    print(f"# temp read failed: {exc}", file=sys.stderr,
                          flush=True)

                duty = args.duty if phase == "on" else 0
                link.set_heater(duty, True)
                phase_samples.setdefault(phase, []).append(
                    (now - started, temp_c))

            try:
                last_status = link.transaction()
            except Exception as exc:  # noqa: BLE001
                if now - last_error >= 2.0:
                    print(f"# SPI error: {exc}", file=sys.stderr, flush=True)
                    last_error = now
                time.sleep(0.02)
                continue

            if sample_now:
                status = last_status
                active = status.link_state == p.LINK_ACTIVE
                print(
                    f"{now - started:8.2f},{phase},"
                    f"{'' if temp_c is None else f'{temp_c:.3f}'},"
                    f"{duty},{status.heater_duty},"
                    f"0x{status.heater_flags:02X},"
                    f"0x{status.fault_flags:04X},"
                    f"{'OK' if active else status.link_state},"
                    f"{status.stm32_uptime_ms / 1000.0:.1f},"
                    f"{fmt(read_load1())},{fmt(read_cpu_khz())}",
                    flush=True,
                )
                if status.heater_flags & 0x04:
                    print("# OVERTEMP: stopping step test", file=sys.stderr,
                          flush=True)
                    stop = True
                    break
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"heater_step_test: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            link.set_heater(0, False)
            link.set_thermal_config(int(round(args.setpoint * 1000)),
                                    int(round(args.margin * 1000)),
                                    0, 0, 0, max(args.duty, 1), False,
                                    args.split_heat)
            link.queue_all_off()
            for _ in range(3):
                link.transaction()
                time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup failed: {exc}", file=sys.stderr, flush=True)
        finally:
            link.close()
        print("# HEAT requested OFF", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
