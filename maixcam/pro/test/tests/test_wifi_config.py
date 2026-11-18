"""Tests for runtime WiFi credential handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parents[1]
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

import wifi_config  # noqa: E402


class WifiConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (wifi_config.SSID_PATH, wifi_config.PASS_PATH,
                       wifi_config.NETWORKS_PATH)
        wifi_config.SSID_PATH = root / "wifi.ssid"
        wifi_config.PASS_PATH = root / "wifi.pass"
        wifi_config.NETWORKS_PATH = root / "wifi.networks"

    def tearDown(self):
        wifi_config.SSID_PATH, wifi_config.PASS_PATH, \
            wifi_config.NETWORKS_PATH = self._saved
        self.tmp.cleanup()

    def test_validate_rejects_unusable_input(self):
        for ssid, password in (("", ""), ("x" * 33, ""), ("ss id", "short"),
                               ("ss\nid", "longenough")):
            with self.assertRaises(ValueError):
                wifi_config.validate(ssid, password)

    def test_validate_accepts_open_and_wpa(self):
        self.assertEqual(wifi_config.validate("Jl521522", "drc4361502"),
                         ("Jl521522", "drc4361502"))
        # empty password = open network
        self.assertEqual(wifi_config.validate("JLU.PC", ""), ("JLU.PC", ""))

    def test_write_persists_and_syncs_candidate_list(self):
        state = wifi_config.write("MyPhone", "supersecret42")
        self.assertEqual(state["configured_ssid"], "MyPhone")
        self.assertFalse(state["open_network"])
        self.assertEqual(wifi_config.SSID_PATH.read_text().strip(), "MyPhone")
        self.assertEqual(wifi_config.PASS_PATH.read_text().strip(),
                         "supersecret42")
        # The retry script prefers the candidate list, so it must not go stale.
        self.assertIn("MyPhone|supersecret42",
                      wifi_config.NETWORKS_PATH.read_text())
        self.assertEqual(state["candidates"], ["MyPhone"])

    def test_write_keeps_a_backup_of_the_previous_credentials(self):
        wifi_config.write("First", "password1")
        wifi_config.write("Second", "password2")
        self.assertEqual(wifi_config.SSID_PATH.read_text().strip(), "Second")
        backup = wifi_config.SSID_PATH.with_suffix(
            wifi_config.SSID_PATH.suffix + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text().strip(), "First")

    def test_open_network_round_trip(self):
        state = wifi_config.write("Campus", "")
        self.assertTrue(state["open_network"])
        self.assertEqual(state["configured_ssid"], "Campus")

    def test_candidate_list_is_reported_for_handover(self):
        wifi_config.NETWORKS_PATH.write_text(
            "# 顺序即优先级\nJl521522|drc4361502\nJLU.PC|\n")
        state = wifi_config.read_state()
        self.assertEqual(state["candidates"], ["Jl521522", "JLU.PC"])

    def test_state_is_json_friendly_and_tolerates_missing_files(self):
        state = wifi_config.read_state()
        self.assertEqual(state["configured_ssid"], "")
        self.assertEqual(state["candidates"], [])
        self.assertIn("ip", state)
        self.assertIn("current_ssid", state)


if __name__ == "__main__":
    unittest.main()
