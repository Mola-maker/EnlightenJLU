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
* The P term uses a filtered 45 s slope and a 90 s look-ahead.  The SG2002
  sensor lags the heater plate by a long time; without this prediction the
  controller overshoots by 3-4 C before a conventional P term reacts.

The result is still a normal PI controller near the setpoint, but it has a
sensible starting point and cannot accumulate a large hidden duty during the
initial heat-up.
"""
from __future__ import annotations

from collections import deque
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
    predicted_c: float = 0.0
    slope_c_per_s: float = 0.0


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
        hold_duty: int = 170,
        warmup_band_c: float = 4.0,
        warmup_hysteresis_c: float = 0.5,
        integral_window_c: float = 0.6,
        integral_slow_factor: float = 0.05,
        slope_window_s: float = 45.0,
        slope_lookahead_s: float = 90.0,
        slope_deadband_c_per_s: float = 0.003,
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
        if integral_window_c < 0:
            raise ValueError("integral_window_c must be >= 0")
        if not 0.0 <= integral_slow_factor <= 1.0:
            raise ValueError("integral_slow_factor must be 0..1")
        if slope_window_s < 3.0:
            raise ValueError("slope_window_s must be >= 3")
        if slope_lookahead_s < 0.0:
            raise ValueError("slope_lookahead_s must be >= 0")
        if slope_deadband_c_per_s < 0.0:
            raise ValueError("slope_deadband_c_per_s must be >= 0")

        self.setpoint_c = float(setpoint_c)
        self.margin_c = float(margin_c)
        self.kp = float(kp)
        self.ki = float(ki)
        self.kd = float(kd)
        self.max_duty = int(max_duty)
        self.hold_duty = min(int(hold_duty), int(max_duty))
        self.warmup_band_c = float(warmup_band_c)
        self.warmup_hysteresis_c = float(warmup_hysteresis_c)
        self.integral_window_c = float(integral_window_c)
        self.integral_slow_factor = float(integral_slow_factor)
        self.slope_window_s = float(slope_window_s)
        self.slope_lookahead_s = float(slope_lookahead_s)
        self.slope_deadband_c_per_s = float(slope_deadband_c_per_s)

        self._samples = deque()
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

    def _slope(self, now):
        """Least-squares slope over the recent sensor window."""
        while self._samples and self._samples[0][0] < now - self.slope_window_s:
            self._samples.popleft()
        if len(self._samples) < 5:
            return 0.0
        t0 = self._samples[0][0]
        times = [sample[0] - t0 for sample in self._samples]
        temps = [sample[1] for sample in self._samples]
        if times[-1] < 5.0:
            return 0.0
        mean_t = sum(times) / len(times)
        mean_temp = sum(temps) / len(temps)
        denom = sum((t - mean_t) ** 2 for t in times)
        if denom <= 0.0:
            return 0.0
        slope = sum((t - mean_t) * (temp - mean_temp)
                    for t, temp in zip(times, temps)) / denom
        if abs(slope) < self.slope_deadband_c_per_s:
            return 0.0
        return slope

    def update(self, temp_c: float, now: float) -> PiOutput:
        """Compute one control step from a valid temperature sample."""
        temp_c = float(temp_c)
        self._samples.append((now, temp_c))
        slope = self._slope(now)
        # The heater/plate -> SG2002 path has a long lag.  Predict only
        # heating, never cooling, and use the predicted temperature for P.
        # A cooling prediction would turn the heater on earlier, which is not
        # what the long-lag plant needs.
        predicted_c = temp_c + max(0.0, slope) * self.slope_lookahead_s
        error_c = self.setpoint_c - temp_c
        p_term = self.kp * (self.setpoint_c - predicted_c)
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
                # While approaching from far away, only trim the integrator
                # very slowly.  The measured SG2002 sensor often sits on a
                # quantised plateau for many seconds; full Ki there winds the
                # output up and causes the slow 41 -> 44 C overshoot seen on
                # 2026-11-17.  Close to the target, integration is normal.
                integral_rate = self.ki * error_c
                if abs(error_c) > self.integral_window_c:
                    integral_rate *= self.integral_slow_factor
                candidate = self._integral + integral_rate * dt
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
            predicted_c=predicted_c,
            slope_c_per_s=slope,
        )
        return self._last
