"""Host-side tests for the software-SPI4 response recovery."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

import stm32_protocol as p  # noqa: E402
from spi4_bootstrap import parse_spi4_response  # noqa: E402


def status_frame():
    status_value = struct.pack(
        "<IIIIHiHBBHB",
        7, 3, 0x4, 1234, p.FAULT_SPI,
        43046, 100, 1, p.LINK_ACTIVE, 42, 2,
    ) + bytes((0, 2, 0))
    caps_value = struct.pack(
        "<BBBBI", 1, 3, 15, 0,
        p.FEATURE_PERFORM | p.FEATURE_TEMP | p.FEATURE_HEARTBEAT)
    payload = (p.tlv(p.TLV_STATUS, status_value) +
               p.tlv(p.TLV_CAPS, caps_value))
    return p.build_frame(p.TYPE_STATUS, 9, 0x55AA1234, payload)


class Spi4ResponseTest(unittest.TestCase):
    def test_normal_frame(self):
        status, caps = parse_spi4_response(status_frame())
        self.assertEqual(status.link_state, p.LINK_ACTIVE)
        self.assertEqual(status.last_control_seq, 7)
        self.assertEqual(caps["glyph_count"], 3)

    def test_one_leading_glitch_byte_is_recovered(self):
        frame = status_frame()
        shifted = b"\x5a" + frame[:p.FRAME_SIZE - 1]
        status, caps = parse_spi4_response(shifted)
        self.assertEqual(status.link_state, p.LINK_ACTIVE)
        self.assertEqual(status.glyph_state, (0, 2, 0))
        self.assertEqual(caps["pwm_channel_count"], 15)

    def test_all_zero_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_spi4_response(bytes(p.FRAME_SIZE))

    def test_bad_crc_is_rejected(self):
        frame = bytearray(status_frame())
        frame[20] ^= 0x01
        with self.assertRaises(ValueError):
            parse_spi4_response(bytes(frame))

    def test_short_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_spi4_response(status_frame()[:-1])


if __name__ == "__main__":
    unittest.main()
