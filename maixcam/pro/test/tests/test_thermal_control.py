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
        self.assertEqual(snap["no_heat_junction_c"], 21.0)
        self.assertEqual(snap["setpoint_c"], 43.0)
        self.assertEqual(snap["margin_c"], 3.0)

    def test_gains_are_derived_from_the_physics_inputs(self):
        """The operator supplies ambient + no-heat junction, nothing else."""
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.configure({
            "ambient_c": -20.0,
            "no_heat_junction_c": 5.0,
            "max_duty": 1000,
        })
        tuning = snap["tuning"]
        self.assertTrue(snap["gains_derived"])
        self.assertAlmostEqual(tuning["self_heat_c"], 25.0)
        self.assertEqual(snap["hold_duty"], tuning["hold_duty"])
        self.assertGreater(snap["hold_duty"], 500)   # cold start needs a lot
        self.assertAlmostEqual(snap["kp"], tuning["kp"], places=2)
        self.assertAlmostEqual(snap["ki"], tuning["ki"], places=3)
        self.assertTrue(tuning["reachable"])
        # The config actually sent to the STM32 uses the derived gains.
        ctrl.ensure_link_config()
        self.assertEqual(link.config[2], int(round(snap["kp"] * 100)))

    def test_gain_fields_from_the_web_are_ignored(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.configure({"ambient_c": -4.0, "no_heat_junction_c": 21.0,
                               "kp": 400.0, "ki": 50.0, "kd": 20.0})
        self.assertEqual(list(snap["ignored_fields"]), ["kp", "ki", "kd"])
        self.assertNotEqual(snap["kp"], 400.0)
        self.assertTrue(snap["gains_derived"])
        self.assertAlmostEqual(snap["kp"], snap["tuning"]["kp"], places=2)

    def test_engineering_override_still_allows_manual_gains(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.configure({"engineering_override": True,
                               "ambient_c": -4.0, "no_heat_junction_c": 21.0,
                               "kp": 80.0, "ki": 2.0, "hold_duty": 300})
        self.assertFalse(snap["gains_derived"])
        self.assertAlmostEqual(snap["kp"], 80.0, places=2)
        self.assertAlmostEqual(snap["ki"], 2.0, places=3)
        self.assertEqual(snap["hold_duty"], 300)
        self.assertEqual(list(snap["ignored_fields"]), [])

    def test_legacy_anchor_field_maps_to_no_heat_junction(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.configure({"anchor_c": 12.0, "ambient_c": -20.0})
        self.assertEqual(snap["no_heat_junction_c"], 12.0)
        self.assertEqual(snap["anchor_c"], 12.0)

    def test_unreachable_setpoint_surfaces_in_snapshot(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        snap = ctrl.configure({"ambient_c": -20.0, "no_heat_junction_c": 5.0,
                               "max_duty": 500})
        self.assertFalse(snap["tuning"]["reachable"])
        self.assertTrue(snap["tuning"]["warnings"])
        self.assertEqual(snap["hold_duty"], 500)

    def test_derived_config_is_sent_to_the_stm32(self):
        link = FakeLink()
        ctrl = ThermalController(link)
        ctrl.configure({"ambient_c": -20.0, "no_heat_junction_c": 5.0,
                        "max_duty": 1000, "enabled": False})
        ctrl.ensure_link_config()
        setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100, max_duty, \
            enabled, split = link.config
        self.assertEqual(setpoint_mc, 43000)
        self.assertEqual(max_duty, 1000)
        self.assertGreater(kp_x100, 0)
        self.assertGreater(ki_x100, 0)
        self.assertEqual(kd_x100, 0)
        self.assertFalse(enabled)


if __name__ == "__main__":
    unittest.main()
