"""Read-only WiFi status adapter for the MaixCAM2 web console.

MaixCAM2 is normally provisioned by the vendor image / MaixVision.  The
console shows the current SSID and IP, but intentionally does not rewrite the
system wpa_supplicant configuration from a browser.  This keeps the web
console focused on lamp/thermal control and avoids locking the user out of the
board after changing networks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

IFACE = "wlan0"
WPAS_CONF = Path("/etc/wpa_supplicant.conf")


def _run(args) -> str:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _configured_ssid() -> str:
    try:
        text = WPAS_CONF.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'^\s*ssid\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def _link_state() -> tuple[str, str]:
    ssid = ""
    for line in _run(["iw", "dev", IFACE, "link"]).splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
            break
    if not ssid:
        for line in _run(["wpa_cli", "-i", IFACE, "status"]).splitlines():
            line = line.strip()
            if line.startswith("ssid="):
                ssid = line.split("=", 1)[1].strip()
                break
    ip = ""
    for token in _run(["ip", "-4", "-brief", "addr", "show", IFACE]).split():
        if "/" in token and token[0].isdigit():
            ip = token.split("/", 1)[0]
            break
    return ssid, ip


def read_state() -> dict:
    current_ssid, ip = _link_state()
    configured = _configured_ssid()
    return {
        "configured_ssid": configured or current_ssid,
        "open_network": False,
        "candidates": [configured] if configured else [],
        "current_ssid": current_ssid or None,
        "ip": ip or None,
    }


def write(ssid: str, password: str) -> dict:
    raise ValueError(
        "MaixCAM2 的网络配置请在系统设置/MaixVision 中修改；"
        "本中控台只读显示当前 SSID 和 IP")


def apply_async() -> None:
    return None
