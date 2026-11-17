"""Thermal PID state for the local MaixCAM console.

The PID is intentionally small and explicit: proportional + integral action
with conditional integration/anti-windup, and an optional derivative term.
The SG2002 junction sensor updates at roughly 1 Hz, so the default Kd is 0.
The STM32 receives a safety envelope and a HEAT duty request; it still has the
final over-temperature/stale-temperature/link-loss cutoff.
"""
from __future__ import annotations

import math
import time
from collections import deque

DEFAULT_SETPOINT_C = 43.0
DEFAULT_MARGIN_C = 3.0
DEFAULT_AMBIENT_C = -4.0
DEFAULT_ANCHOR_C = 21.0
DEFAULT_KP = 60.0
DEFAULT_KI = 1.0
DEFAULT_KD = 0.0
DEFAULT_MAX_DUTY = 300
HISTORY_SECONDS = 300


def _clamp(value, low, high):
    return max(low, min(high, value))


class ThermalController:
    def __init__(self, link):
        self.link = link
        self.setpoint_c = DEFAULT_SETPOINT_C
        self.margin_c = DEFAULT_MARGIN_C
        self.ambient_c = DEFAULT_AMBIENT_C
        self.anchor_c = DEFAULT_ANCHOR_C
        self.kp = DEFAULT_KP
        self.ki = DEFAULT_KI
        self.kd = DEFAULT_KD
        self.max_duty = DEFAULT_MAX_DUTY
        self.enabled = False
        self.determined_at = "2026-11-17"
        self.config_dirty = True
        self.config_sent_at = 0.0

        self.temp_c = None
        self.temp_mc = 0
        self.temp_valid = False
        self.temp_source = 0xFF
        self.last_sample_at = 0.0
        self.last_temp_at = 0.0
        self.output = 0.0
        self.p_term = 0.0
        self.i_term = 0.0
        self.d_term = 0.0
        self.overtemp = False
        self.history = deque(maxlen=HISTORY_SECONDS)
        self._integral = 0.0
        self._previous_temp_c = None
        self._previous_tick = None

    # ---- configuration / HTTP commands -------------------------------
    def configure(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")

        def number(name, current, low, high):
            if name not in payload:
                return current
            try:
                value = float(payload[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            if not low <= value <= high:
                raise ValueError(f"{name} must be {low}..{high}")
            return value

        self.setpoint_c = number("setpoint_c", self.setpoint_c, 0.0, 100.0)
        self.margin_c = number("margin_c", self.margin_c, 0.0, 20.0)
        self.ambient_c = number("ambient_c", self.ambient_c, -40.0, 60.0)
        self.anchor_c = number("anchor_c", self.anchor_c, -40.0, 100.0)
        self.kp = number("kp", self.kp, 0.0, 500.0)
        self.ki = number("ki", self.ki, 0.0, 100.0)
        self.kd = number("kd", self.kd, 0.0, 50.0)
        self.max_duty = int(round(number(
            "max_duty", self.max_duty, 0.0, 1000.0)))
        if "enabled" in payload:
            self.enabled = bool(payload["enabled"])
        if "determined_at" in payload:
            self.determined_at = str(payload["determined_at"])[:40]
        self.config_dirty = True
        if not self.enabled:
            self._shutdown_output()
        return self.snapshot()

    def ensure_link_config(self):
        """Send the actuator safety envelope once per revision, with retry.

        The software SPI4 bus occasionally drops a complete transaction; a
        periodic retry makes a lost THERMAL_CONFIG recoverable without
        requiring the user to toggle the heater.
        """
        now = time.monotonic()
        if not self.config_dirty and now - self.config_sent_at < 5.0:
            return
        self.link.set_thermal_config(
            int(round(self.setpoint_c * 1000)),
            int(round(self.margin_c * 1000)),
            int(round(self.kp * 100)),
            int(round(self.ki * 100)),
            int(round(self.kd * 100)),
            self.max_duty,
            self.enabled,
        )
        self.config_dirty = False
        self.config_sent_at = now

    def link_lost(self):
        self.config_dirty = True
        self.config_sent_at = 0.0
        self._shutdown_output()

    # ---- 1 Hz sensor / PID -------------------------------------------
    def sample(self, temp_c, temp_mc, source, now=None):
        now = time.monotonic() if now is None else now
        self.temp_c = float(temp_c)
        self.temp_mc = int(temp_mc)
        self.temp_valid = True
        self.temp_source = int(source)
        self.last_sample_at = now
        self.last_temp_at = now

        if not self.enabled:
            self._shutdown_output()
        else:
            self._step(now)

        upper = self.setpoint_c + self.margin_c
        lower = self.setpoint_c - self.margin_c
        self.history.append({
            "t": round(now, 3),
            "temp_c": round(self.temp_c, 3),
            "setpoint_c": round(self.setpoint_c, 3),
            "upper_c": round(upper, 3),
            "lower_c": round(lower, 3),
            "duty": round(self.output, 1),
            "p": round(self.p_term, 2),
            "i": round(self.i_term, 2),
            "d": round(self.d_term, 2),
            "enabled": self.enabled,
            "overtemp": self.overtemp,
        })
        return self.snapshot()

    def sample_failed(self, now=None):
        now = time.monotonic() if now is None else now
        self.temp_valid = False
        self.temp_c = None
        self.last_sample_at = now
        self._shutdown_output()
        return self.snapshot()

    def _shutdown_output(self):
        self.output = 0.0
        self.p_term = 0.0
        self.i_term = 0.0
        self.d_term = 0.0
        self._integral = 0.0
        self.link.set_heater(0, False)

    def _step(self, now):
        if self.temp_c is None:
            self._shutdown_output()
            return

        dt = 1.0 if self._previous_tick is None else max(
            0.05, now - self._previous_tick)
        error_c = self.setpoint_c - self.temp_c
        p_term = self.kp * error_c
        d_term = 0.0
        if self.kd > 0.0 and self._previous_temp_c is not None:
            d_term = -self.kd * (self.temp_c - self._previous_temp_c) / dt

        candidate = self._integral + self.ki * error_c * dt
        candidate = _clamp(candidate, 0.0, float(self.max_duty))
        unsaturated = p_term + candidate + d_term
        if ((unsaturated > self.max_duty and error_c > 0.0) or
                (unsaturated < 0.0 and error_c < 0.0)):
            candidate = self._integral
        self._integral = _clamp(candidate, 0.0, float(self.max_duty))
        self.i_term = self._integral
        self.p_term = p_term
        self.d_term = d_term
        self.output = _clamp(p_term + self.i_term + d_term,
                             0.0, float(self.max_duty))

        upper = self.setpoint_c + self.margin_c
        release = upper - 1.0
        if self.temp_c >= upper:
            self.overtemp = True
            self.output = 0.0
            self._integral = 0.0
            self.i_term = 0.0
        elif self.overtemp and self.temp_c <= release:
            self.overtemp = False

        self._previous_temp_c = self.temp_c
        self._previous_tick = now
        self.link.set_heater(int(round(self.output)), self.enabled)

    # ---- UI state -----------------------------------------------------
    def snapshot(self):
        history = list(self.history)
        slope = None
        if len(history) >= 2:
            first = history[0]
            last = history[-1]
            dt = max(0.001, last["t"] - first["t"])
            slope = (last["temp_c"] - first["temp_c"]) / dt
        return {
            "temp_c": (None if self.temp_c is None
                       else round(self.temp_c, 3)),
            "temp_mc": self.temp_mc,
            "temp_valid": self.temp_valid,
            "temp_source": self.temp_source,
            "temp_age_ms": (None if not self.last_temp_at else
                            int(max(0.0, time.monotonic() -
                                    self.last_temp_at) * 1000)),
            "setpoint_c": round(self.setpoint_c, 3),
            "margin_c": round(self.margin_c, 3),
            "upper_c": round(self.setpoint_c + self.margin_c, 3),
            "lower_c": round(self.setpoint_c - self.margin_c, 3),
            "ambient_c": round(self.ambient_c, 3),
            "anchor_c": round(self.anchor_c, 3),
            "determined_at": self.determined_at,
            "kp": self.kp,
            "ki": self.ki,
            "kd": self.kd,
            "max_duty": self.max_duty,
            "enabled": self.enabled,
            "duty": round(self.output, 1),
            "p": round(self.p_term, 2),
            "i": round(self.i_term, 2),
            "d": round(self.d_term, 2),
            "overtemp": self.overtemp,
            "slope_c_per_s": (None if slope is None
                              else round(slope, 5)),
            "no_response": bool(
                self.enabled and len(history) >= 20 and
                abs(slope or 0.0) < 0.02 and
                max((h["duty"] for h in history[-20:]), default=0) > 50),
        }
