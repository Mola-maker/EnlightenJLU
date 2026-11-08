"""Host-side tests for the web lamp controller."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

import stm32_protocol as p  # noqa: E402
from web_control import LampController  # noqa: E402


class FakeLink:
    def __init__(self, features):
        self.caps = {"features": features}
        self.status = SimpleNamespace(
            link_state=p.LINK_ACTIVE,
            active_mask=0,
            glyph_state=(0, 0, 0),
            fault_flags=0,
            last_control_seq=0,
            last_event_seq=0,
            temp_mc=0,
            temp_valid=False,
        )
        self.lamps = None
        self.performs = []
        self.all_off = 0

    def set_lamps(self, duties):
        self.lamps = list(duties)

    def clear_lamps(self):
        self.lamps = None

    def queue_perform(self, mask, action, profile_id=0, value=0):
        self.performs.append((mask, action))
        return len(self.performs)

    def queue_all_off(self):
        self.all_off += 1


class WebControllerTest(unittest.TestCase):
    def test_direct_effect_writes_all_13_duties(self):
        link = FakeLink(features=p.FEATURE_DIRECT)
        ctrl = LampController(link)
        ctrl.command({"command": "effect", "effect": "solid",
                      "target": 0x1FFF, "brightness": 50})
        ctrl.tick(link.status)
        self.assertEqual(link.lamps, [500] * 13)
        self.assertTrue(ctrl.snapshot()["direct_ready"])

    def test_target_masks_only_selected_lamp(self):
        link = FakeLink(features=p.FEATURE_DIRECT)
        ctrl = LampController(link)
        ctrl.command({"command": "effect", "effect": "solid",
                      "target": 0b100, "brightness": 20})
        ctrl.tick(link.status)
        self.assertEqual(link.lamps[0], 0)
        self.assertEqual(link.lamps[1], 0)
        self.assertEqual(link.lamps[2], 200)
        self.assertEqual(sum(1 for x in link.lamps if x), 1)

    def test_preview_still_computes_without_direct_firmware(self):
        link = FakeLink(features=0)
        ctrl = LampController(link)
        ctrl.command({"command": "effect", "effect": "solid",
                      "target": 0x1FFF, "brightness": 30})
        ctrl.tick(link.status)
        self.assertIsNone(link.lamps)
        self.assertEqual(ctrl.snapshot()["duties"], [300] * 13)

    def test_kws_command_returns_to_standby(self):
        link = FakeLink(features=p.FEATURE_DIRECT)
        ctrl = LampController(link)
        ctrl.command({"command": "kws"})
        self.assertEqual(ctrl.mode, "kws")
        self.assertEqual(link.performs[-1], (4, p.ACTION_STANDBY))

    def test_off_command_sends_all_off(self):
        link = FakeLink(features=p.FEATURE_DIRECT)
        ctrl = LampController(link)
        ctrl.command({"command": "off"})
        ctrl.tick(link.status)
        self.assertEqual(link.all_off, 1)

    def test_invalid_brightness_is_rejected(self):
        link = FakeLink(features=p.FEATURE_DIRECT)
        ctrl = LampController(link)
        with self.assertRaises(ValueError):
            ctrl.command({"command": "effect", "brightness": 101})


if __name__ == "__main__":
    unittest.main()
