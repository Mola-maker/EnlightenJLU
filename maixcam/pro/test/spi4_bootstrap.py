"""Bring up the MaixCAM / MaixCAM-Pro software SPI4 bus.

A22-A25 are wired to the ``spi4@gpio`` platform device, not to the
hardware SPI3 controller exposed as ``/dev/spidev3.0``.  On some images
the ``spi_gpio`` driver does not bind to that platform device during
boot, so ``/dev/spidev4.0`` never appears.  Binding is idempotent and
requires root, which is the normal account used by the MaixCAM link.
"""

from __future__ import annotations

import errno
import subprocess
import time
from pathlib import Path

SPI4_DEVICE = Path("/dev/spidev4.0")
SPI4_PLATFORM_DEVICE = "spi4@gpio"
SPI4_DRIVER_DIR = Path("/sys/bus/platform/drivers/spi_gpio")
SPI4_PLATFORM_DIR = Path("/sys/bus/platform/devices") / SPI4_PLATFORM_DEVICE
KERNEL_MODULE_DIR = Path("/mnt/system/ko")

SPI4_PINS = {
    "A22": "SPI4_SCK",
    "A23": "SPI4_MISO",
    "A24": "SPI4_CS",
    "A25": "SPI4_MOSI",
}


def configure_spi4_pins() -> None:
    """Select the SPI4 pin functions on A22-A25."""
    from maix import err, pinmap

    for pin, function in SPI4_PINS.items():
        if pinmap.get_pin_function(pin) == function:
            continue
        result = pinmap.set_pin_function(pin, function)
        if (result != err.Err.ERR_NONE and
                pinmap.get_pin_function(pin) != function):
            raise RuntimeError(
                f"pinmap {pin} -> {function} failed: {result}")


def _load_module(filename: str) -> None:
    """Load a kernel module if it is not already present.

    A ``File exists`` error from insmod is harmless: it means the module
    is already loaded.  The caller checks the driver directory afterwards
    and raises a useful error if the bind can still not be performed.
    """
    path = KERNEL_MODULE_DIR / filename
    if not path.exists():
        return
    subprocess.run(
        ["insmod", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def parse_spi4_response(raw: bytes):
    """Parse a 64-byte SPI4 response, tolerating one leading glitch byte.

    The bit-banged SPI4 path occasionally returns one extra leading byte
    and drops the final reserved zero byte.  The CRC still covers the
    shifted frame, so dropping the first byte, re-appending the reserved
    zero, and validating the CRC is safe.  Unrecoverable frames are left
    for the caller's retry logic.
    """
    import stm32_protocol as p

    if len(raw) != p.FRAME_SIZE:
        raise ValueError(f"short SPI response: {len(raw)} bytes")

    candidates = [raw]
    if raw[:2] != b"\x5a\xa5" and raw[1:3] == b"\x5a\xa5":
        candidates.append(raw[1:] + b"\x00")

    last_error = None
    for candidate in candidates:
        try:
            return p.parse_status(candidate)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise ValueError(f"invalid SPI response: {last_error}") from last_error


def ensure_spi4_device(timeout: float = 2.0) -> Path:
    """Ensure ``/dev/spidev4.0`` exists and return its path.

    The SPI4 controller is a kernel bit-banged SPI bus backed by
    ``spi4@gpio``.  Depending on module load order it can be left
    unbound; writing the platform device name to the driver's ``bind``
    file creates the SPI master and its spidev child.
    """
    if SPI4_DEVICE.exists():
        return SPI4_DEVICE

    if not SPI4_PLATFORM_DIR.exists():
        raise RuntimeError(
            "spi4@gpio platform device is missing; "
            "this image does not expose software SPI4")

    if not SPI4_DRIVER_DIR.exists():
        _load_module("spi-bitbang.ko")
        _load_module("spi-gpio.ko")

    if not SPI4_DRIVER_DIR.exists():
        raise RuntimeError(
            "spi_gpio driver is unavailable; cannot create /dev/spidev4.0")

    bound = SPI4_DRIVER_DIR / SPI4_PLATFORM_DEVICE
    if not bound.exists():
        try:
            (SPI4_DRIVER_DIR / "bind").write_text(
                f"{SPI4_PLATFORM_DEVICE}\n", encoding="ascii")
        except OSError as exc:
            # EBUSY means another process won the race and bound it.
            if exc.errno != errno.EBUSY:
                raise RuntimeError(
                    f"failed to bind {SPI4_PLATFORM_DEVICE}: {exc}") from exc

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if SPI4_DEVICE.exists():
            return SPI4_DEVICE
        time.sleep(0.02)

    raise RuntimeError(
        "spi4@gpio is bound but /dev/spidev4.0 did not appear; "
        "check udev/devtmpfs and the kernel log")
