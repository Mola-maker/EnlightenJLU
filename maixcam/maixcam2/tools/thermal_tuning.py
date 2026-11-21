"""Derive the heater PID gains from what the operator can actually measure.

The web console used to expose ``Kp``/``Ki``/``Kd`` directly.  Those are
implementation details of the controller; the operator only knows the test
environment.  This module turns the two observable numbers

* ``ambient_c``           -- temperature of the test environment (chamber/room)
* ``no_heat_junction_c``  -- junction temperature the board settles at with the
                             heater switched off (SoC self-heating included)

into everything the controller needs: the steady-state feedforward duty, the
warm-up band and the PI gains.  Nobody has to tune a control loop at -20 C.

Plant model (first order + dead time; duty in per mille of full scale):

    T_j(t) = T_amb + self_heat + g * (u / 1000) * (1 - exp(-(t - theta)/tau))

* ``self_heat`` = ``no_heat_junction_c - ambient_c``.  It is *not* a constant:
  the SoC's own dissipation depends on the airflow and heat sink of the test
  environment, so it is an operator input instead of a baked-in assumption.
* ``g`` = :data:`HEAT_GAIN_C_PER_FULL_DUTY`, the steady-state rise above the
  no-heat baseline produced by 1000 per mille of heater duty.  Device
  calibration constant, overridable per experiment.
* ``tau`` / ``theta`` = plant time constant / dead time.  Device constants.

Calibration of ``g`` from the 2026-11-17 bring-up (ambient -4 C, no-heat
junction 21 C, 350 per mille held 43 C):

    (43 - 21) / 0.35 = 62.9  ->  g = 63 C per full-scale duty

The same data is the consistency check for the gain rule below: feeding
``ambient_c=-4, no_heat_junction_c=21`` reproduces the hand-tuned
``hold_duty=350, Kp=60, Ki~1.4`` that was validated on the bench, so the
derivation is anchored in measurement rather than in a guessed formula.

Gain rule (lambda tuning for the first-order + dead-time model above, with
``lambda`` chosen roughly ``tau/5`` so the closed loop is ~5x faster than the
open-loop plant but still far slower than the 1 Hz sensor):

    Kp = tau / (K * (lambda + theta))        K = g / 1000  [C per per mille]
    Ki = Kp / (lambda + theta)               [per mille per (C * s)]

``Kd`` stays 0: the SG2002 sensor updates at ~1 Hz in ~0.35 C steps and the
heater->plate->sensor path has a long lag, so a raw derivative term mostly
amplifies quantisation noise.  ``thermal_pi`` already provides the necessary
lead through its 45 s filtered slope and 90 s look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Steady-state junction rise above the no-heat baseline at 1000 per mille.
HEAT_GAIN_C_PER_FULL_DUTY = 63.0
#: Plant time constant [s] measured during the 2026-11-17 bring-up.
PLANT_TIME_CONSTANT_S = 180.0
#: Heater -> plate -> sensor dead time [s].
PLANT_DEAD_TIME_S = 10.0
#: Desired closed-loop time constant [s] (~tau/5).
CLOSED_LOOP_LAMBDA_S = 35.0
#: Fraction of ``max_duty`` above which the setpoint counts as unreachable.
REACHABLE_DUTY_FRACTION = 0.9

KP_BOUNDS = (5.0, 200.0)
KI_BOUNDS = (0.05, 5.0)
WARMUP_BAND_BOUNDS = (1.0, 6.0)
#: Warm-up band as a fraction of the travel from the no-heat point to the setpoint.
WARMUP_BAND_FRACTION = 0.15


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class ThermalTuning:
    """Everything derived from the operator's two inputs."""

    ambient_c: float
    no_heat_junction_c: float
    setpoint_c: float
    max_duty: int
    heat_gain_c_per_full_duty: float
    self_heat_c: float
    required_rise_c: float
    required_duty: float
    hold_duty: int
    kp: float
    ki: float
    kd: float
    warmup_band_c: float
    reachable: bool
    expected_max_c: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ambient_c": round(self.ambient_c, 3),
            "no_heat_junction_c": round(self.no_heat_junction_c, 3),
            "setpoint_c": round(self.setpoint_c, 3),
            "max_duty": self.max_duty,
            "heat_gain_c_per_full_duty": round(
                self.heat_gain_c_per_full_duty, 2),
            "self_heat_c": round(self.self_heat_c, 3),
            "required_rise_c": round(self.required_rise_c, 3),
            "required_duty": round(self.required_duty, 1),
            "hold_duty": self.hold_duty,
            "kp": round(self.kp, 2),
            "ki": round(self.ki, 3),
            "kd": round(self.kd, 3),
            "warmup_band_c": round(self.warmup_band_c, 2),
            "reachable": self.reachable,
            "expected_max_c": round(self.expected_max_c, 2),
            "warnings": list(self.warnings),
        }


def derive_tuning(
    ambient_c: float,
    no_heat_junction_c: float,
    setpoint_c: float,
    max_duty: int,
    *,
    heat_gain_c_per_full_duty: float = HEAT_GAIN_C_PER_FULL_DUTY,
    time_constant_s: float = PLANT_TIME_CONSTANT_S,
    dead_time_s: float = PLANT_DEAD_TIME_S,
    lambda_s: float = CLOSED_LOOP_LAMBDA_S,
) -> ThermalTuning:
    """Compute feedforward duty, warm-up band and PI gains.

    Raises ``ValueError`` for inputs the controller cannot use (bad ranges,
    non-positive heater gain); physics problems such as an unreachable
    setpoint are reported through ``warnings``/``reachable`` instead.
    """
    ambient_c = float(ambient_c)
    no_heat_junction_c = float(no_heat_junction_c)
    setpoint_c = float(setpoint_c)
    max_duty = int(max_duty)
    heat_gain = float(heat_gain_c_per_full_duty)

    if not -40.0 <= ambient_c <= 60.0:
        raise ValueError("ambient_c must be -40..60")
    if not -40.0 <= no_heat_junction_c <= 150.0:
        raise ValueError("no_heat_junction_c must be -40..150")
    if not 0.0 <= setpoint_c <= 100.0:
        raise ValueError("setpoint_c must be 0..100")
    if not 0 <= max_duty <= 1000:
        raise ValueError("max_duty must be 0..1000")
    if heat_gain <= 0.0:
        raise ValueError("heat_gain_c_per_full_duty must be > 0")
    if time_constant_s <= 0.0 or dead_time_s < 0.0 or lambda_s <= 0.0:
        raise ValueError("plant time constants must be > 0")

    warnings: list[str] = []

    self_heat_c = no_heat_junction_c - ambient_c
    if self_heat_c < 0.0:
        warnings.append(
            f"无加热结温({no_heat_junction_c:.1f} °C) 低于环境温度"
            f"({ambient_c:.1f} °C)，自热温升按 0 处理，请复核输入")
        self_heat_c = 0.0

    required_rise_c = setpoint_c - no_heat_junction_c
    if required_rise_c <= 0.0:
        # The board already sits at or above the target without heating.
        warnings.append(
            f"无加热结温已不低于目标({setpoint_c:.1f} °C)，加热器保持 0")
        required_duty = 0.0
        required_rise_c = min(required_rise_c, 0.0)
    else:
        required_duty = 1000.0 * required_rise_c / heat_gain

    expected_max_c = no_heat_junction_c + heat_gain * max_duty / 1000.0
    duty_ceiling = REACHABLE_DUTY_FRACTION * max_duty
    reachable = required_rise_c <= 0.0 or required_duty <= duty_ceiling
    if required_rise_c > 0.0 and not reachable:
        warnings.append(
            f"目标 {setpoint_c:.1f} °C 在 max_duty={max_duty} per mille 下不可达："
            f"预计最高约 {expected_max_c:.1f} °C（需 "
            f"{required_duty:.0f} per mille）")

    hold_duty = int(round(clamp(required_duty, 0.0, float(max_duty))))

    plant_gain = heat_gain / 1000.0          # degC per per mille
    closed_loop_s = lambda_s + dead_time_s
    kp = time_constant_s / (plant_gain * closed_loop_s)
    ki = kp / closed_loop_s
    kd = 0.0

    kp_clamped = clamp(kp, *KP_BOUNDS)
    if kp_clamped != kp:
        warnings.append(
            f"Kp 由模型算得 {kp:.1f}，超出安全区间 "
            f"{KP_BOUNDS[0]:.0f}..{KP_BOUNDS[1]:.0f}，已限幅")
        kp = kp_clamped
    ki_clamped = clamp(ki, *KI_BOUNDS)
    if ki_clamped != ki:
        warnings.append(
            f"Ki 由模型算得 {ki:.3f}，超出安全区间 "
            f"{KI_BOUNDS[0]:.2f}..{KI_BOUNDS[1]:.2f}，已限幅")
        ki = ki_clamped

    if required_rise_c > 0.0:
        band = clamp(WARMUP_BAND_FRACTION * required_rise_c,
                     *WARMUP_BAND_BOUNDS)
    else:
        band = WARMUP_BAND_BOUNDS[0]

    return ThermalTuning(
        ambient_c=ambient_c,
        no_heat_junction_c=no_heat_junction_c,
        setpoint_c=setpoint_c,
        max_duty=max_duty,
        heat_gain_c_per_full_duty=heat_gain,
        self_heat_c=self_heat_c,
        required_rise_c=required_rise_c,
        required_duty=required_duty,
        hold_duty=hold_duty,
        kp=kp,
        ki=ki,
        kd=kd,
        warmup_band_c=band,
        reachable=reachable,
        expected_max_c=expected_max_c,
        warnings=tuple(warnings),
    )
