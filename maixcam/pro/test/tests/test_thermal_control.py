"""Host-side tests for the thermal console controller."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from thermal_control import ThermalController  # noqa: E402


class FakeLink:
    def __init__(self):
        self.config = None
        self.heater = None

    def set_thermal_config(self, *args):
        self.config = args

    def set_heater(self, duty, enable=True):
        self.heater = (duty, enable)


class ThermalControlTest(unittest.TestCase):
    def test_disabled_never_requests_heat(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        ctrl.sample(25.0, 25000, 0, now=1.0)
        self.assertEqual(link.heater, (0, False))
        self.assertFalse(ctrl.snapshot()["enabled"])

    def test_enabled_below_setpoint_requests_bounded_duty(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        ctrl.configure({"enabled": True, "max_duty": 300})
        ctrl.sample(25.0, 25000, 0, now=1.0)
        self.assertTrue(link.heater[1])
        self.assertGreaterEqual(link.heater[0], 0)
        self.assertLessEqual(link.heater[0], 300)

    def test_over_temperature_forces_zero(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        ctrl.configure({"enabled": True, "setpoint_c": 43, "margin_c": 3,
                        "max_duty": 1000})
        ctrl.sample(47.0, 47000, 0, now=1.0)
        self.assertEqual(link.heater, (0, True))
        self.assertTrue(ctrl.snapshot()["overtemp"])

    def test_anchor_context_is_reported(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.snapshot()
        self.assertEqual(snap["ambient_c"], -4.0)
        self.assertEqual(snap["anchor_c"], 21.0)
        self.assertEqual(snap["setpoint_c"], 43.0)
        self.assertEqual(snap["margin_c"], 3.0)


if __name__ == "__main__":
    unittest.main()
