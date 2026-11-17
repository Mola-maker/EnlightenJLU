"""Protocol tests for the host-side thermal actuator."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

import stm32_protocol as p  # noqa: E402


class ThermalProtocolTest(unittest.TestCase):
    def test_thermal_config_layout(self):
        payload = p.thermal_config(43000, 3000, 6000, 100, 0, 300, True)
        type_, length = payload[0], payload[1]
        self.assertEqual(type_, p.TLV_THERMAL_CONFIG)
        self.assertEqual(length, 18)
        values = struct.unpack("<IIHHHHBB", payload[2:])
        self.assertEqual(values[0], 43000)
        self.assertEqual(values[1], 3000)
        self.assertEqual(values[2], 6000)
        self.assertEqual(values[3], 100)
        self.assertEqual(values[4], 0)
        self.assertEqual(values[5], 300)
        self.assertEqual(values[6], 1)
        self.assertEqual(values[7], 0)

    def test_heater_layout_and_clamp(self):
        payload = p.heater(1500, True)
        self.assertEqual(payload[0], p.TLV_HEATER)
        self.assertEqual(payload[1], 4)
        duty, enable, reserved = struct.unpack("<HBB", payload[2:])
        self.assertEqual((duty, enable, reserved), (1000, 1, 0))

    def test_status_exposes_heater_fields(self):
        status_value = struct.pack(
            "<IIIIHiHBBHB",
            7, 3, 0x4, 1234, 0,
            43046, 100, 1, p.LINK_ACTIVE,
            300,  (0x10 << 1) | 0,
        ) + bytes((0, 2, 0))
        caps_value = struct.pack(
            "<BBBBI", 1, 3, 15, 0,
            p.FEATURE_PERFORM | p.FEATURE_TEMP | p.FEATURE_THERMAL_PID)
        frame = p.build_frame(
            p.TYPE_STATUS, 9, 0x55AA1234,
            p.tlv(p.TLV_STATUS, status_value) + p.tlv(p.TLV_CAPS, caps_value))
        status, caps = p.parse_status(frame)
        self.assertEqual(status.heater_duty, 300)
        self.assertEqual(status.heater_flags, 0x10)
        self.assertEqual(status.temp_source, 0)
        self.assertTrue(caps["features"] & p.FEATURE_THERMAL_PID)


if __name__ == "__main__":
    unittest.main()
