"""Tests for the operator-facing thermal tuning derivation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from thermal_tuning import (  # noqa: E402
    HEAT_GAIN_C_PER_FULL_DUTY,
    derive_tuning,
)


class ThermalTuningTest(unittest.TestCase):
    def test_reference_point_reproduces_bench_tuning(self):
        """-4 C ambient / 21 C no-heat is the 2026-11-17 validated point.

        Hand tuning there was hold_duty=350 per mille, Kp=60, Ki~1.0 and a
        4 C warm-up band.  The derivation must land on the same numbers,
        otherwise it cannot be trusted in the chamber.
        """
        tuning = derive_tuning(ambient_c=-4.0, no_heat_junction_c=21.0,
                               setpoint_c=43.0, max_duty=500)
        self.assertAlmostEqual(tuning.self_heat_c, 25.0)
        self.assertAlmostEqual(tuning.required_rise_c, 22.0)
        self.assertAlmostEqual(tuning.required_duty, 349.2, places=1)
        self.assertEqual(tuning.hold_duty, 349)
        self.assertGreaterEqual(tuning.kp, 55.0)
        self.assertLessEqual(tuning.kp, 70.0)
        self.assertGreaterEqual(tuning.ki, 1.0)
        self.assertLessEqual(tuning.ki, 1.6)
        self.assertAlmostEqual(tuning.warmup_band_c, 3.3, places=1)
        self.assertTrue(tuning.reachable)
        self.assertEqual(tuning.warnings, ())

    def test_colder_environment_needs_more_feedforward_duty(self):
        warm = derive_tuning(-4.0, 21.0, 43.0, 1000)
        cold = derive_tuning(-20.0, 5.0, 43.0, 1000)
        self.assertGreater(cold.required_duty, warm.required_duty)
        self.assertGreater(cold.hold_duty, warm.hold_duty)
        # Gains depend on the plant, not the environment: same heater, same Kp.
        self.assertAlmostEqual(cold.kp, warm.kp, places=3)
        self.assertAlmostEqual(cold.ki, warm.ki, places=3)

    def test_duty_scales_with_heat_gain(self):
        weak = derive_tuning(-20.0, 5.0, 43.0, 1000,
                            heat_gain_c_per_full_duty=40.0)
        strong = derive_tuning(-20.0, 5.0, 43.0, 1000,
                              heat_gain_c_per_full_duty=100.0)
        self.assertGreater(weak.required_duty, strong.required_duty)
        # A weaker heater needs a larger Kp for the same closed-loop speed.
        self.assertGreater(weak.kp, strong.kp)

    def test_unreachable_setpoint_is_reported_not_raised(self):
        tuning = derive_tuning(-20.0, 5.0, 43.0, max_duty=500)
        self.assertFalse(tuning.reachable)
        self.assertTrue(any("不可达" in w for w in tuning.warnings))
        self.assertEqual(tuning.hold_duty, 500)   # clipped to the limit
        self.assertLess(tuning.expected_max_c, 43.0)

    def test_board_already_above_setpoint_requests_no_heat(self):
        tuning = derive_tuning(-4.0, 50.0, 43.0, 500)
        self.assertEqual(tuning.hold_duty, 0)
        self.assertEqual(tuning.required_duty, 0.0)
        self.assertTrue(any("不低于目标" in w for w in tuning.warnings))

    def test_no_heat_below_ambient_is_a_warning(self):
        tuning = derive_tuning(20.0, 18.0, 43.0, 500)
        self.assertEqual(tuning.self_heat_c, 0.0)
        self.assertTrue(any("低于环境温度" in w for w in tuning.warnings))

    def test_expected_max_uses_calibration(self):
        tuning = derive_tuning(-20.0, 5.0, 43.0, 1000)
        self.assertAlmostEqual(
            tuning.expected_max_c,
            5.0 + HEAT_GAIN_C_PER_FULL_DUTY, places=6)

    def test_rejects_unusable_inputs(self):
        with self.assertRaises(ValueError):
            derive_tuning(-80.0, 5.0, 43.0, 500)      # ambient out of range
        with self.assertRaises(ValueError):
            derive_tuning(-20.0, 5.0, 43.0, 1200)     # duty out of range
        with self.assertRaises(ValueError):
            derive_tuning(-20.0, 5.0, 43.0, 500,
                          heat_gain_c_per_full_duty=0.0)

    def test_dict_is_json_friendly(self):
        payload = derive_tuning(-20.0, 5.0, 43.0, 500).as_dict()
        for key in ("hold_duty", "kp", "ki", "kd", "reachable",
                    "required_duty", "warnings", "expected_max_c"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["warnings"], list)


if __name__ == "__main__":
    unittest.main()
