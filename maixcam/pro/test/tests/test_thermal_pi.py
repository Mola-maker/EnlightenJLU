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
        self.assertAlmostEqual(out.i_term, 351.5)
        self.assertAlmostEqual(out.output, 441.5)

    def test_integral_is_frozen_when_output_is_saturated(self):
        pi = ThermalPI(setpoint_c=43, max_duty=600, hold_duty=590,
                       kp=60, ki=1, warmup_band_c=0)
        for second in range(60):
            out = pi.update(42.5, now=float(second))
        self.assertAlmostEqual(out.i_term, 590.0)
        self.assertEqual(out.output, 600)

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
        self.assertAlmostEqual(out.i_term, 351.9)


if __name__ == "__main__":
    unittest.main()
