#!/usr/bin/env python3
"""Live SG2002 junction-temperature monitor for the PID bench.

This is the command-line sensor check that replaces using the Web UI for
debugging.  It reads the MaixCAM SoC temperature (sysfs by default) at a fixed
interval and prints one line per sample with a short rolling window.

Examples:
    python3 -u tools/temp_monitor.py
    python3 -u tools/temp_monitor.py --interval 0.5 --count 20
    python3 -u tools/temp_monitor.py --format csv --count 10
    python3 -u tools/temp_monitor.py --source reg --format json --count 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
APP_DIR = TEST_DIR.parent / "app"
for _path in (TEST_DIR, APP_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sg2002_temp import sample as read_temperature  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Live SG2002 junction temperature monitor")
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="sample interval in seconds (default: 1.0)")
    parser.add_argument(
        "--count", type=int, default=0,
        help="number of samples, 0 means run until Ctrl-C (default: 0)")
    parser.add_argument(
        "--source", choices=("auto", "sysfs", "reg"), default="auto",
        help="temperature source (default: auto, sysfs first)")
    parser.add_argument(
        "--format", choices=("text", "csv", "json"), default="text",
        help="output format (default: text)")
    parser.add_argument(
        "--window", type=int, default=30,
        help="rolling statistics window in samples (default: 30)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.count < 0:
        parser.error("--count must be >= 0")
    if args.window < 1:
        parser.error("--window must be >= 1")
    return args


def csv_header():
    return ("timestamp,temp_c,temp_mc,source,raw,min_c,max_c,avg_c,"
            "trend_c_per_s")


def main(argv=None):
    args = parse_args(argv)
    history = deque(maxlen=args.window)
    previous_temp = None
    previous_tick = None
    samples = 0

    if args.format == "csv":
        print(csv_header(), flush=True)

    try:
        while args.count == 0 or samples < args.count:
            started = time.monotonic()
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            try:
                item = read_temperature(args.source)
            except Exception as exc:  # noqa: BLE001
                print(f"{timestamp} temperature read failed: {exc}",
                      file=sys.stderr, flush=True)
                time.sleep(min(args.interval, 1.0))
                continue

            temp_c = float(item["temp_c"])
            temp_mc = int(item["temp_mc"])
            source = str(item.get("source", "?"))
            raw = item.get("raw")
            history.append(temp_c)

            tick = time.monotonic()
            trend = None
            if previous_temp is not None:
                dt = tick - previous_tick
                if dt > 0:
                    trend = (temp_c - previous_temp) / dt
            previous_temp = temp_c
            previous_tick = tick

            lo = min(history)
            hi = max(history)
            avg = sum(history) / len(history)
            samples += 1

            if args.format == "csv":
                raw_text = "" if raw is None else str(raw)
                trend_text = "" if trend is None else f"{trend:+.4f}"
                print(f"{timestamp},{temp_c:.3f},{temp_mc},{source},"
                      f"{raw_text},{lo:.3f},{hi:.3f},{avg:.3f},{trend_text}",
                      flush=True)
            elif args.format == "json":
                print(json.dumps({
                    "timestamp": timestamp,
                    "temp_c": round(temp_c, 3),
                    "temp_mc": temp_mc,
                    "source": source,
                    "raw": raw,
                    "min_c": round(lo, 3),
                    "max_c": round(hi, 3),
                    "avg_c": round(avg, 3),
                    "trend_c_per_s": (None if trend is None
                                      else round(trend, 4)),
                }, ensure_ascii=False), flush=True)
            else:
                trend_text = "warming" if trend is None else f"{trend:+.3f} °C/s"
                raw_text = "" if raw is None else f" raw={raw}"
                print(f"{timestamp}  {temp_c:7.3f} °C  "
                      f"src={source}{raw_text}  "
                      f"min={lo:.3f} max={hi:.3f} avg={avg:.3f}  "
                      f"trend={trend_text}", flush=True)

            if args.count and samples >= args.count:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr, flush=True)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
