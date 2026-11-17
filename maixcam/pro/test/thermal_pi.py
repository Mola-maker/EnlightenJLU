"""Deterministic warm-start PI controller for the slow heater plant.

The SG2002 junction sensor updates at about 1 Hz and is quantised in roughly
0.35 C steps.  A conventional PI controller that integrates all the way from
cold start stores several hundred permille of integral while the output is
saturated at ``max_duty``.  The heater then keeps that energy after the
temperature reaches the setpoint, producing a large, slow overshoot.

This module uses three simple rules:

* Outside ``warmup_band_c`` the heater runs open loop at ``max_duty`` and the
  integrator is held at ``hold_duty``; it does not wind up.
* When the measured temperature enters the band, the integrator starts from
  the measured holding duty instead of the accumulated saturation value.
* Integral action is conditional: it is frozen whenever the total PI output
  is already saturated in the same direction as the current error.

The result is still a normal PI controller near the setpoint, but it has a
sensible starting point and cannot accumulate a large hidden duty during the
initial heat-up.
"""
from __future__ import annotations

from dataclasses import dataclass


def clamp(value, low, high):
    return max(low, min(high, value))


@dataclass(frozen=True)
class PiOutput:
    error_c: float
    p_term: float
    i_term: float
    d_term: float
    output: float
    warmup: bool
    overtemp: bool


class ThermalPI:
    """PI state machine shared by the command-line thermal controller."""

    def __init__(
        self,
        setpoint_c: float = 43.0,
        margin_c: float = 3.0,
        kp: float = 60.0,
        ki: float = 1.0,
        kd: float = 0.0,
        max_duty: int = 600,
        hold_duty: int = 350,
        warmup_band_c: float = 2.0,
        warmup_hysteresis_c: float = 0.5,
    ):
        if max_duty < 0 or max_duty > 1000:
            raise ValueError("max_duty must be 0..1000")
        if hold_duty < 0 or hold_duty > 1000:
            raise ValueError("hold_duty must be 0..1000")
        if margin_c < 0:
            raise ValueError("margin_c must be >= 0")
        if warmup_band_c < 0:
            raise ValueError("warmup_band_c must be >= 0")
        if warmup_hysteresis_c < 0:
            raise ValueError("warmup_hysteresis_c must be >= 0")

        self.setpoint_c = float(setpoint_c)
        self.margin_c = float(margin_c)
        self.kp = float(kp)
        self.ki = float(ki)
        self.kd = float(kd)
        self.max_duty = int(max_duty)
        self.hold_duty = min(int(hold_duty), int(max_duty))
        self.warmup_band_c = float(warmup_band_c)
        self.warmup_hysteresis_c = float(warmup_hysteresis_c)

        self._integral = float(self.hold_duty)
        self._previous_temp_c = None
        self._previous_tick = None
        self._warmup = True
        self._overtemp = False
        self._last = PiOutput(0.0, 0.0, 0.0, 0.0, 0.0, True, False)

    @property
    def last(self) -> PiOutput:
        return self._last

    @property
    def integral(self) -> float:
        return self._integral

    def suspend(self) -> None:
        """Return to a safe zero request without discarding learned state.

        Sensor-read failures are handled separately by the caller.  Keeping
        the integrator and warm-up state makes a short read failure resume
        smoothly; the STM32 still enforces its stale-temperature cut-off.
        """
        self._last = PiOutput(
            error_c=0.0,
            p_term=0.0,
            i_term=0.0,
            d_term=0.0,
            output=0.0,
            warmup=self._warmup,
            overtemp=self._overtemp,
        )

    def update(self, temp_c: float, now: float) -> PiOutput:
        """Compute one control step from a valid temperature sample."""
        temp_c = float(temp_c)
        error_c = self.setpoint_c - temp_c
        p_term = self.kp * error_c
        d_term = 0.0

        if (self.kd > 0.0 and self._previous_temp_c is not None and
                self._previous_tick is not None):
            dt_for_d = clamp(now - self._previous_tick, 0.05, 5.0)
            d_term = -self.kd * (temp_c - self._previous_temp_c) / dt_for_d

        upper_c = self.setpoint_c + self.margin_c
        release_c = upper_c - 1.0
        if temp_c >= upper_c:
            self._overtemp = True
        elif self._overtemp and temp_c <= release_c:
            self._overtemp = False

        if self._overtemp:
            # A hardware over-temperature event always wins.  Start the next
            # recovery from zero integral; at 45 C a seeded holding duty would
            # still be too much heat.
            self._warmup = False
            self._integral = 0.0
            output = 0.0
        else:
            band = self.warmup_band_c
            rearm_at = self.setpoint_c - band - self.warmup_hysteresis_c
            if band > 0.0 and temp_c <= rearm_at:
                self._warmup = True

            if band > 0.0 and self._warmup and temp_c <= self.setpoint_c - band:
                # Open-loop approach.  Hold the integrator at the expected
                # steady duty so it cannot accumulate the saturated output.
                self._integral = float(self.hold_duty)
                output = float(self.max_duty)
            else:
                if self._warmup:
                    # First sample inside the band: hand over from open loop
                    # to PI at the holding duty instead of at max_duty.
                    self._warmup = False
                    self._integral = float(self.hold_duty)

                dt = (1.0 if self._previous_tick is None else
                      clamp(now - self._previous_tick, 0.05, 5.0))
                candidate = self._integral + self.ki * error_c * dt
                candidate = clamp(candidate, 0.0, float(self.max_duty))
                unsaturated = p_term + candidate + d_term
                if ((unsaturated > self.max_duty and error_c > 0.0) or
                        (unsaturated < 0.0 and error_c < 0.0)):
                    candidate = self._integral
                self._integral = clamp(candidate, 0.0, float(self.max_duty))
                output = clamp(p_term + self._integral + d_term,
                               0.0, float(self.max_duty))

        self._previous_temp_c = temp_c
        self._previous_tick = now
        self._last = PiOutput(
            error_c=error_c,
            p_term=p_term,
            i_term=self._integral,
            d_term=d_term,
            output=output,
            warmup=self._warmup,
            overtemp=self._overtemp,
        )
        return self._last
