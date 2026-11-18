"""Runtime WiFi credentials for the board (handover friendly).

Every user brings their own phone hotspot, so the SSID/password must be
writable at runtime instead of being baked into the image.  Three ways end up
here:

* the web console (``POST /api/wifi``) -- works while you are connected to the
  board by any means (USB network, same LAN, ...);
* editing ``/boot/wifi.ssid`` + ``/boot/wifi.pass`` on the FAT partition from a
  PC, for the very first connection when nothing is reachable yet;
* the shell, via :func:`write` / ``/etc/init.d/S99wifi-retry``.

Writes are atomic-ish: the current files are copied to ``*.bak`` first, then
``S99wifi-retry`` is started detached so the HTTP response can be flushed
before the link goes down.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

SSID_PATH = Path("/etc/wifi.ssid")
PASS_PATH = Path("/etc/wifi.pass")
NETWORKS_PATH = Path("/etc/wifi.networks")
RETRY_SCRIPT = "/etc/init.d/S99wifi-retry"
IFACE = "wlan0"

SSID_MAX_BYTES = 32
PSK_MIN_LEN = 8
PSK_MAX_LEN = 63


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def validate(ssid: str, password: str) -> tuple[str, str]:
    """Return the normalised pair or raise ``ValueError``."""
    ssid = (ssid or "").strip()
    password = password or ""
    if not ssid:
        raise ValueError("SSID 不能为空")
    if len(ssid.encode("utf-8")) > SSID_MAX_BYTES:
        raise ValueError(f"SSID 超过 {SSID_MAX_BYTES} 字节")
    if "\n" in ssid or "\n" in password:
        raise ValueError("不能包含换行")
    if password and not PSK_MIN_LEN <= len(password) <= PSK_MAX_LEN:
        raise ValueError(
            f"WPA 密码需 {PSK_MIN_LEN}-{PSK_MAX_LEN} 位，开放网络请留空")
    return ssid, password


def _link_state() -> dict:
    """Best-effort current association from ``iw``; never raises."""
    info = {"ssid": None, "ip": None}
    try:
        out = subprocess.run(["iw", "dev", IFACE, "link"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            info["ssid"] = line.split(":", 1)[1].strip()
    try:
        out = subprocess.run(["ip", "-4", "-brief", "addr", "show", IFACE],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for token in out.split():
        if "/" in token and token[0].isdigit():
            info["ip"] = token.split("/")[0]
            break
    return info


def read_state() -> dict:
    """What the console shows in the network card."""
    ssid = _read(SSID_PATH)
    password = _read(PASS_PATH)
    if not ssid:
        ssid = _read(Path("/boot/wifi.ssid"))
        password = _read(Path("/boot/wifi.pass"))
    candidates = []
    for path in (NETWORKS_PATH, Path("/boot/wifi.networks")):
        text = _read(path)
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("|", 1)[0].strip()
            if name:
                candidates.append(name)
        if candidates:
            break
    link = _link_state()
    return {
        "configured_ssid": ssid,
        "open_network": not password,
        "candidates": candidates,
        "current_ssid": link["ssid"],
        "ip": link["ip"],
    }


def write(ssid: str, password: str) -> dict:
    """Persist the credentials (and make the candidate list agree)."""
    ssid, password = validate(ssid, password)
    for path in (SSID_PATH, PASS_PATH, NETWORKS_PATH):
        try:
            if path.exists():
                path.replace(path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    SSID_PATH.write_text(ssid + "\n", encoding="utf-8")
    PASS_PATH.write_text(password + "\n", encoding="utf-8")
    # The retry script prefers the candidate list, so keep it in sync instead of
    # letting a stale list silently win.
    try:
        NETWORKS_PATH.write_text(
            f"# 由 Web 控制台写入（顺序即优先级）\n{ssid}|{password}\n",
            encoding="utf-8")
        NETWORKS_PATH.chmod(0o600)
    except OSError:
        pass
    return read_state()


def apply_async(delay: float = 1.0) -> None:
    """Reload WiFi in the background so the HTTP reply can be sent first."""

    def _run():
        time.sleep(delay)
        try:
            subprocess.Popen(
                ["/bin/sh", RETRY_SCRIPT, "start"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            pass

    threading.Thread(target=_run, name="wifi-apply", daemon=True).start()
