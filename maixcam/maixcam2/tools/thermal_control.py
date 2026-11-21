"""Thermal control state for the local MaixCAM console.

The operator only supplies the physics of the test environment:

* ``ambient_c``          -- temperature of the chamber / room
* ``no_heat_junction_c`` -- junction temperature the board settles at with the
                            heater switched off (SoC self-heating included)

:mod:`thermal_tuning` turns those into the steady-state feedforward duty, the
warm-up band and the PI gains.  ``Kp``/``Ki``/``Kd`` are therefore *outputs* of
this module, not user inputs; they can only be forced through the explicit
``engineering_override`` flag, which exists for bench bring-up and the
command-line controller, not for the web console.

The controller delegates the actual PI state machine to ``thermal_pi`` so the
web console and command-line controller use the same warm-start/anti-windup
rules.  The SG2002 junction sensor updates at roughly 1 Hz, so Kd stays 0.
The STM32 receives a safety envelope and a HEAT duty request; it still has the
final over-temperature/stale-temperature/link-loss cutoff.
"""
from __future__ import annotations

import time
from collections import deque

from thermal_pi import ThermalPI
from thermal_tuning import HEAT_GAIN_C_PER_FULL_DUTY, derive_tuning

DEFAULT_SETPOINT_C = 43.0
DEFAULT_MARGIN_C = 3.0
DEFAULT_AMBIENT_C = -4.0
#: Junction temperature with the heater off; ``anchor_c`` is the legacy name.
DEFAULT_NO_HEAT_JUNCTION_C = 21.0
DEFAULT_ANCHOR_C = DEFAULT_NO_HEAT_JUNCTION_C
DEFAULT_MAX_DUTY = 500
DEFAULT_WARMUP_HYSTERESIS_C = 0.5
#: Only used when ``engineering_override`` is enabled (bench bring-up).
DEFAULT_KP = 60.0
DEFAULT_KI = 1.0
DEFAULT_KD = 0.0
DEFAULT_WARMUP_REARM = False
DEFAULT_INTEGRAL_WINDOW_C = 0.6
DEFAULT_INTEGRAL_SLOW_FACTOR = 0.05
DEFAULT_SLOPE_WINDOW_S = 45.0
DEFAULT_SLOPE_LOOKAHEAD_S = 90.0
DEFAULT_SLOPE_DEADBAND_C_PER_S = 0.003
HISTORY_SECONDS = 300


class ThermalController:
    def __init__(self, link):
        self.link = link
        self.setpoint_c = DEFAULT_SETPOINT_C
        self.margin_c = DEFAULT_MARGIN_C
        self.ambient_c = DEFAULT_AMBIENT_C
        self.no_heat_junction_c = DEFAULT_NO_HEAT_JUNCTION_C
        self.heat_gain_c_per_full_duty = HEAT_GAIN_C_PER_FULL_DUTY
        self.engineering_override = False
        self.ignored_fields = ()
        # Derived by _retune(); never user input unless engineering_override.
        self.kp = DEFAULT_KP
        self.ki = DEFAULT_KI
        self.kd = DEFAULT_KD
        self.max_duty = DEFAULT_MAX_DUTY
        self.hold_duty = 0
        self.warmup_band_c = 0.0
        self.tuning = None
        self.warmup_hysteresis_c = DEFAULT_WARMUP_HYSTERESIS_C
        self.warmup_rearm = DEFAULT_WARMUP_REARM
        self.integral_window_c = DEFAULT_INTEGRAL_WINDOW_C
        self.integral_slow_factor = DEFAULT_INTEGRAL_SLOW_FACTOR
        self.slope_window_s = DEFAULT_SLOPE_WINDOW_S
        self.slope_lookahead_s = DEFAULT_SLOPE_LOOKAHEAD_S
        self.slope_deadband_c_per_s = DEFAULT_SLOPE_DEADBAND_C_PER_S
        self.split_heat = False
        self.enabled = False
        self.determined_at = "2026-11-17"
        self.config_dirty = True
        self.config_sent_at = 0.0
        self._retune()

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
        self._pi = self._new_pi()

    def _new_pi(self):
        return ThermalPI(
            setpoint_c=self.setpoint_c,
            margin_c=self.margin_c,
            kp=self.kp,
            ki=self.ki,
            kd=self.kd,
            max_duty=self.max_duty,
            hold_duty=min(self.hold_duty, self.max_duty),
            warmup_band_c=self.warmup_band_c,
            warmup_hysteresis_c=self.warmup_hysteresis_c,
            warmup_rearm=self.warmup_rearm,
            integral_window_c=self.integral_window_c,
            integral_slow_factor=self.integral_slow_factor,
            slope_window_s=self.slope_window_s,
            slope_lookahead_s=self.slope_lookahead_s,
            slope_deadband_c_per_s=self.slope_deadband_c_per_s,
        )

    # ---- configuration / HTTP commands -------------------------------
    def _pi_parameters(self):
        return (
            self.setpoint_c, self.margin_c, self.kp, self.ki, self.kd,
            self.max_duty, self.hold_duty, self.warmup_band_c,
            self.warmup_hysteresis_c, self.warmup_rearm,
            self.integral_window_c, self.integral_slow_factor,
            self.slope_window_s, self.slope_lookahead_s,
            self.slope_deadband_c_per_s,
        )

    def _retune(self):
        """Re-derive the feedforward duty and gains from the physics inputs."""
        self.tuning = derive_tuning(
            self.ambient_c, self.no_heat_junction_c, self.setpoint_c,
            self.max_duty,
            heat_gain_c_per_full_duty=self.heat_gain_c_per_full_duty)
        if self.engineering_override:
            self.hold_duty = min(self.hold_duty, self.max_duty)
        else:
            self.kp = self.tuning.kp
            self.ki = self.tuning.ki
            self.kd = self.tuning.kd
            self.hold_duty = self.tuning.hold_duty
            self.warmup_band_c = self.tuning.warmup_band_c

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

        if "engineering_override" in payload:
            self.engineering_override = bool(payload["engineering_override"])

        old_pi_parameters = self._pi_parameters()

        # --- operator inputs: the physics of the test environment --------
        self.setpoint_c = number("setpoint_c", self.setpoint_c, 0.0, 100.0)
        self.margin_c = number("margin_c", self.margin_c, 0.0, 20.0)
        self.ambient_c = number("ambient_c", self.ambient_c, -40.0, 60.0)
        no_heat = self.no_heat_junction_c
        if "no_heat_junction_c" in payload:
            no_heat = number("no_heat_junction_c", no_heat, -40.0, 150.0)
        elif "anchor_c" in payload:   # legacy field name
            no_heat = number("anchor_c", no_heat, -40.0, 150.0)
        self.no_heat_junction_c = no_heat
        self.max_duty = int(round(number(
            "max_duty", self.max_duty, 0.0, 1000.0)))
        # Device calibration; exposed so a different heater/plate can be used,
        # but it is not a control-loop gain.
        self.heat_gain_c_per_full_duty = number(
            "heat_gain_c_per_full_duty", self.heat_gain_c_per_full_duty,
            1.0, 500.0)

        # --- plant-agnostic limits, still engineering constants ----------
        self.warmup_hysteresis_c = number(
            "warmup_hysteresis_c", self.warmup_hysteresis_c, 0.0, 5.0)
        if "warmup_rearm" in payload:
            self.warmup_rearm = bool(payload["warmup_rearm"])
        self.integral_window_c = number(
            "integral_window_c", self.integral_window_c, 0.0, 10.0)
        self.integral_slow_factor = number(
            "integral_slow_factor", self.integral_slow_factor, 0.0, 1.0)
        self.slope_window_s = number(
            "slope_window_s", self.slope_window_s, 3.0, 180.0)
        self.slope_lookahead_s = number(
            "slope_lookahead_s", self.slope_lookahead_s, 0.0, 600.0)
        self.slope_deadband_c_per_s = number(
            "slope_deadband_c_per_s", self.slope_deadband_c_per_s,
            0.0, 1.0)
        if "enabled" in payload:
            self.enabled = bool(payload["enabled"])
        if "split_heat" in payload:
            self.split_heat = bool(payload["split_heat"])
        if "determined_at" in payload:
            self.determined_at = str(payload["determined_at"])[:40]

        # --- derived values (never user input unless overridden) --------
        self._retune()
        if self.engineering_override:
            self.kp = number("kp", self.kp, 0.0, 500.0)
            self.ki = number("ki", self.ki, 0.0, 100.0)
            self.kd = number("kd", self.kd, 0.0, 50.0)
            self.hold_duty = int(round(number(
                "hold_duty", self.hold_duty, 0.0, 1000.0)))
            self.hold_duty = min(self.hold_duty, self.max_duty)
            self.warmup_band_c = number(
                "warmup_band_c", self.warmup_band_c, 0.0, 20.0)
            self.ignored_fields = ()
        else:
            self.ignored_fields = tuple(
                name for name in ("kp", "ki", "kd", "hold_duty",
                                  "warmup_band_c")
                if name in payload)

        if self._pi_parameters() != old_pi_parameters:
            self._pi = self._new_pi()

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
            self.split_heat,
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
        self.overtemp = False
        self._pi = self._new_pi()
        self.link.set_heater(0, False)

    def _step(self, now):
        if self.temp_c is None:
            self._shutdown_output()
            return

        out = self._pi.update(self.temp_c, now)
        self.p_term = out.p_term
        self.i_term = out.i_term
        self.d_term = out.d_term
        self.output = out.output
        self.overtemp = out.overtemp
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
            "no_heat_junction_c": round(self.no_heat_junction_c, 3),
            # legacy alias kept for older UI builds and log scrapers
            "anchor_c": round(self.no_heat_junction_c, 3),
            "heat_gain_c_per_full_duty": round(
                self.heat_gain_c_per_full_duty, 2),
            "determined_at": self.determined_at,
            # derived, read-only for the operator: see thermal_tuning.py
            "kp": round(self.kp, 2),
            "ki": round(self.ki, 3),
            "kd": round(self.kd, 3),
            "gains_derived": not self.engineering_override,
            "ignored_fields": list(self.ignored_fields),
            "tuning": (None if self.tuning is None
                       else self.tuning.as_dict()),
            "max_duty": self.max_duty,
            "hold_duty": self.hold_duty,
            "warmup_band_c": self.warmup_band_c,
            "warmup_hysteresis_c": self.warmup_hysteresis_c,
            "warmup_rearm": self.warmup_rearm,
            "integral_window_c": self.integral_window_c,
            "integral_slow_factor": self.integral_slow_factor,
            "slope_window_s": self.slope_window_s,
            "slope_lookahead_s": self.slope_lookahead_s,
            "slope_deadband_c_per_s": self.slope_deadband_c_per_s,
            "split_heat": self.split_heat,
            "predicted_c": round(self._pi.last.predicted_c, 3),
            "pi_slope_c_per_s": round(self._pi.last.slope_c_per_s, 5),
            "warmup": self._pi.last.warmup,
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
