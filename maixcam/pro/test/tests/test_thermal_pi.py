"""Tests for the warm-start thermal PI state machine."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from thermal_pi import ThermalPI  # noqa: E402


class ThermalPiTest(unittest.TestCase):
    def test_warmup_uses_max_duty_without_integral_windup(self):
        pi = ThermalPI(setpoint_c=43, max_duty=600, hold_duty=350,
                       warmup_band_c=2)
        for second in range(30):
            out = pi.update(35.0, now=float(second))
            self.assertEqual(out.output, 600)
            self.assertEqual(out.i_term, 350)
            self.assertTrue(out.warmup)

    def test_handover_starts_from_holding_duty(self):
        pi = ThermalPI(setpoint_c=43, max_duty=600, hold_duty=350,
                       kp=60, ki=1, warmup_band_c=2)
        pi.update(40.0, now=0.0)  # still warm-up
        out = pi.update(41.5, now=1.0)
        self.assertFalse(out.warmup)
        self.assertAlmostEqual(out.p_term, 90.0)
        self.assertAlmostEqual(out.i_term, 350.075)
        self.assertAlmostEqual(out.output, 440.075)

    def test_integral_is_frozen_when_output_is_saturated(self):
        pi = ThermalPI(setpoint_c=43, max_duty=600, hold_duty=590,
                       kp=60, ki=1, warmup_band_c=0)
        for second in range(60):
            out = pi.update(42.5, now=float(second))
        self.assertAlmostEqual(out.i_term, 590.0)
        self.assertEqual(out.output, 600)

    def test_integral_trims_slowly_while_far_from_target(self):
        pi = ThermalPI(setpoint_c=43, max_duty=500, hold_duty=350,
                       kp=60, ki=1, warmup_band_c=0,
                       integral_window_c=0.6,
                       integral_slow_factor=0.05)
        pi.update(41.5, now=0.0)
        out = pi.update(41.5, now=60.0)
        # 1.5 C error is outside the 0.6 C window; dt is capped at 5 s.
        self.assertAlmostEqual(out.i_term, 350.45)

    def test_positive_slope_uses_predictive_p_term(self):
        pi = ThermalPI(setpoint_c=43, max_duty=500, hold_duty=170,
                       kp=60, warmup_band_c=0,
                       slope_window_s=30, slope_lookahead_s=90,
                       slope_deadband_c_per_s=0.0)
        out = None
        for i in range(20):
            out = pi.update(40.0 + i * 0.05, now=float(i))
        self.assertGreater(out.slope_c_per_s, 0.04)
        self.assertGreater(out.predicted_c, 43.0)
        self.assertLess(out.p_term, 0.0)

    def test_cooling_slope_does_not_predict_extra_heat(self):
        pi = ThermalPI(setpoint_c=43, max_duty=500, hold_duty=170,
                       kp=60, warmup_band_c=0,
                       slope_window_s=30, slope_lookahead_s=90,
                       slope_deadband_c_per_s=0.0)
        out = None
        for i in range(20):
            out = pi.update(42.0 - i * 0.05, now=float(i))
        self.assertLess(out.slope_c_per_s, 0.0)
        self.assertAlmostEqual(out.predicted_c, 42.0 - 19 * 0.05)

    def test_over_temperature_forces_zero_and_does_not_seed_heat(self):
        pi = ThermalPI(setpoint_c=43, margin_c=3, max_duty=600,
                       hold_duty=350, warmup_band_c=2)
        out = pi.update(47.0, now=0.0)
        self.assertTrue(out.overtemp)
        self.assertEqual(out.output, 0)
        self.assertEqual(out.i_term, 0)
        # The release threshold is 45 C; 45.5 C must stay cut off.
        out = pi.update(45.5, now=1.0)
        self.assertTrue(out.overtemp)
        self.assertEqual(out.output, 0)
        # At 44.9 C the latch clears.  At 44.9 C above setpoint the output
        # must still be zero rather than jumping back to hold duty.
        out = pi.update(44.9, now=2.0)
        self.assertFalse(out.overtemp)
        self.assertEqual(out.output, 0)

    def test_warmup_rearms_after_a_large_drop(self):
        pi = ThermalPI(setpoint_c=43, max_duty=600, hold_duty=350,
                       kp=60, ki=1, warmup_band_c=2,
                       warmup_hysteresis_c=0.5)
        pi.update(42.0, now=0.0)
        pi.update(40.4, now=1.0)
        out = pi.update(40.8, now=2.0)
        self.assertTrue(out.warmup)
        self.assertEqual(out.output, 600)
        out = pi.update(41.1, now=3.0)
        self.assertFalse(out.warmup)
        self.assertAlmostEqual(out.i_term, 350.095)


if __name__ == "__main__":
    unittest.main()
