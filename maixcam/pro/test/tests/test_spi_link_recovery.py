"""Regression tests for HELLO/config recovery in the SPI link."""

from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

# spi_link imports spidev even for the payload-only code paths.  Keep this
# test host-runnable without libspidev installed.
if "spidev" not in sys.modules:
    fake_spidev = types.ModuleType("spidev")

    class _FakeSpiDev:
        def open(self, *_args):
            pass

        def close(self):
            pass

    fake_spidev.SpiDev = _FakeSpiDev
    sys.modules["spidev"] = fake_spidev

import stm32_protocol as p  # noqa: E402
from spi_link import Stm32SpiLink  # noqa: E402


def make_link():
    link = Stm32SpiLink.__new__(Stm32SpiLink)
    link.bus = 4
    link.device = 0
    link.frequency = 100_000
    link.retries = 1
    link.retry_delay = 0
    link.settle_time = 0
    link._spi = object()
    link.session_id = 1
    link.seq = 1
    link._event_seq = 1
    link._hello_pending = True
    link._pending_event = None
    link._direct_duties = None
    link._thermal_config = p.thermal_config(43000, 3000, 6000, 100, 0,
                                            600, True)
    link._thermal_config_saved = link._thermal_config
    link._thermal_config_enable = True
    link._thermal_config_pending = False
    link._heater = p.heater(450, True)
    link._temp = None
    link._temp_sent_at = 0.0
    link._last_status = None
    link._last_caps = {"features": p.FEATURE_THERMAL_PID}
    link._started = time.monotonic()
    link._lock = __import__("threading").Lock()
    return link


class SpiLinkRecoveryTest(unittest.TestCase):
    def test_hello_frame_also_carries_safety_config_and_heater(self):
        link = make_link()
        payload = link._payload(time.monotonic())
        self.assertLessEqual(len(payload), p.MAX_PAYLOAD)
        seen = [type_ for type_, _ in p.parse_tlvs(payload)]
        self.assertIn(p.TLV_HELLO, seen)
        self.assertIn(p.TLV_THERMAL_CONFIG, seen)
        self.assertIn(p.TLV_HEATER, seen)

    def test_wait_hello_status_rearms_config_for_next_frame(self):
        link = make_link()
        status = SimpleNamespace(
            link_state=p.LINK_WAIT_HELLO,
            heater_flags=0,
            last_event_seq=0,
        )
        link._transaction_once = lambda payload: (
            status, {"features": p.FEATURE_THERMAL_PID})
        link.transaction()
        self.assertTrue(link._hello_pending)
        self.assertEqual(link._thermal_config, link._thermal_config_saved)

    def test_active_unconfigured_status_rearms_config(self):
        link = make_link()
        status = SimpleNamespace(
            link_state=p.LINK_ACTIVE,
            heater_flags=0,
            last_event_seq=0,
        )
        link._transaction_once = lambda payload: (
            status, {"features": p.FEATURE_THERMAL_PID})
        link.transaction()
        self.assertFalse(link._hello_pending)
        self.assertEqual(link._thermal_config, link._thermal_config_saved)

    def test_active_configured_enabled_status_stops_resending(self):
        link = make_link()
        status = SimpleNamespace(
            link_state=p.LINK_ACTIVE,
            heater_flags=0x11,
            last_event_seq=0,
        )
        link._transaction_once = lambda payload: (
            status, {"features": p.FEATURE_THERMAL_PID})
        link.transaction()
        self.assertFalse(link._hello_pending)
        self.assertIsNone(link._thermal_config)

    def test_unconfigured_configured_only_status_still_requests_enable(self):
        link = make_link()
        status = SimpleNamespace(
            link_state=p.LINK_ACTIVE,
            heater_flags=0x10,
            last_event_seq=0,
        )
        link._transaction_once = lambda payload: (
            status, {"features": p.FEATURE_THERMAL_PID})
        link.transaction()
        self.assertEqual(link._thermal_config, link._thermal_config_saved)


if __name__ == "__main__":
    unittest.main()
