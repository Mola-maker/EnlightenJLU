"""Robust MaixCAM2 SPI master link for the STM32 PWM-Heater board.

This transport is the MaixCAM2 counterpart of the tested MaixCAM Pro
transport in ``maixcam/pro/test/spi_link.py``.  The two boards use different
SPI controllers:

* MaixCAM2: hardware SPI2/CS1 on B20/B19/B21/B18 (``/dev/spidev2.1``).
* MaixCAM Pro: software SPI4 on A22/A23/A24/A25 (``/dev/spidev4.0``).

The communication-hardening rules are intentionally shared with the Pro
transport:

* 100 kHz default, which is the proven production rate for the STM32 link;
* bounded retries with a short delay;
* validate magic/length/CRC, and tolerate the one-byte leading glitch seen
  on some bit-banged/software-SPI captures;
* keep re-sending HELLO until a later STATUS reports LINK_ACTIVE, so an STM32
  reset or a 500 ms link timeout recovers without restarting Python;
* an inter-process ``flock`` so a stale web/thermal process cannot interleave
  frames with the voice process on the same bus.

This module must not import the MaixCAM Pro keyword test.  MaixCAM2 uses the
external SenseVoice model and Chinese text matching; the Pro test's
``ha1 lou2`` / ``wei1 er2 kang1`` pinyin proxies are deliberately absent.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time

import stm32_protocol as p


class SpiTransactionError(RuntimeError):
    """No valid STATUS frame was received after all retries."""


class Stm32SpiLink:
    def __init__(self, spi_id: int | None = None,
                 frequency: int = 100_000,
                 session_id: int | None = None,
                 retries: int = 6,
                 retry_delay: float = 0.05,
                 settle_time: float = 0.1):
        from maix import err, pinmap, spi, sys

        if sys.device_id() == "maixcam2":
            # MaixCAM2 external SPI2, hardware CS1 -> /dev/spidev2.1
            pins = {
                "B20": "SPI2_SCK",
                "B19": "SPI2_MISO",
                "B21": "SPI2_CS1",
                "B18": "SPI2_MOSI",
            }
            default_spi_id = 2
        else:
            # MaixCAM / MaixCAM-Pro SPI4
            pins = {
                "A22": "SPI4_SCK",
                "A23": "SPI4_MISO",
                "A24": "SPI4_CS",
                "A25": "SPI4_MOSI",
            }
            default_spi_id = 4

        self._spi = None
        self.bus_id = default_spi_id if spi_id is None else int(spi_id)
        self.frequency = int(frequency)
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.settle_time = max(0.0, float(settle_time))
        self._bus_lock_fd = None

        self._acquire_bus_lock()
        try:
            for pin, function in pins.items():
                err.check_raise(pinmap.set_pin_function(pin, function),
                                f"pinmap {pin} -> {function} failed")

            self._spi = spi.SPI(self.bus_id,
                                spi.Mode.MASTER,
                                self.frequency,
                                polarity=0, phase=0, bits=8)
            if self.settle_time:
                time.sleep(self.settle_time)
        except Exception:
            self._release_bus_lock()
            raise

        self.session_id = session_id or (
            int.from_bytes(os.urandom(4), "little") or 1
        )
        self.seq = 1
        self._event_seq = 1
        self._hello_pending = True
        self._pending_event: tuple[bytes, int] | None = None
        self._direct_duties = None
        self._temp: bytes | None = None
        self._temp_sent_at = 0.0
        self._last_status = None
        self._last_caps = None
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def _acquire_bus_lock(self) -> None:
        """Prevent two MaixCAM2 processes from clocking the same STM32 bus."""
        path = f"/tmp/stm32-spi{self.bus_id}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"SPI bus {self.bus_id} is already owned by another process "
                f"({path})") from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        self._bus_lock_fd = fd

    def _release_bus_lock(self) -> None:
        fd = getattr(self, "_bus_lock_fd", None)
        self._bus_lock_fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    @property
    def status(self):
        return self._last_status

    @property
    def caps(self):
        return self._last_caps

    def set_temp(self, temp_mc: int, sample_seq: int, age_ms: int,
                 valid: bool = True, source: int = 0):
        with self._lock:
            self._temp = p.temp(temp_mc, sample_seq, age_ms, valid, source)

    def set_lamps(self, duties) -> None:
        if len(duties) != p.LAMP_COUNT:
            raise ValueError(f"expected {p.LAMP_COUNT} lamp duties")
        with self._lock:
            self._direct_duties = [
                max(0, min(1000, int(duty))) for duty in duties
            ]
            self._pending_event = None

    def clear_lamps(self) -> None:
        with self._lock:
            self._direct_duties = None

    def queue_perform(self, glyph_mask: int, action: int,
                      profile_id: int = 0, value: int = 0) -> int:
        with self._lock:
            self._direct_duties = None
            event_seq = self._event_seq
            self._event_seq = (self._event_seq + 1) & 0xFFFFFFFF
            self._pending_event = (
                p.perform(glyph_mask, action, event_seq, profile_id, value),
                event_seq,
            )
            return event_seq

    def queue_all_off(self) -> None:
        with self._lock:
            self._direct_duties = None
            self._pending_event = (p.all_off(), -1)

    def _payload(self, now: float) -> bytes:
        # Caller holds self._lock.
        chunks: list[bytes] = []

        if self._hello_pending:
            chunks.append(p.hello(
                heartbeat_ms=50,
                features=(p.FEATURE_PERFORM | p.FEATURE_TEMP |
                          p.FEATURE_HEARTBEAT)))
        elif self._temp is not None and (now - self._temp_sent_at) >= 1.0:
            chunks.append(self._temp)
            self._temp_sent_at = now

        if self._pending_event is not None:
            chunks.append(self._pending_event[0])
        elif self._direct_duties is not None:
            chunks.append(p.set_lamps(self._direct_duties))
        elif not self._hello_pending:
            chunks.append(p.heartbeat(
                maix_uptime_ms=int((now - self._started) * 1000),
                state_flags=0,
            ))

        payload = b"".join(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            # Temperature is optional; never let it crowd out a control event.
            payload = b"".join(chunk for chunk in chunks if chunk is not self._temp)
        if len(payload) > p.MAX_PAYLOAD:
            raise RuntimeError("SPI v1 payload exceeds 44 bytes")
        return payload

    @staticmethod
    def _parse_status_tolerant(raw: bytes):
        """Parse STATUS, tolerating one leading glitch byte.

        The Pro software-SPI transport occasionally returns one extra leading
        byte and drops the final reserved zero.  The shifted frame still has a
        valid CRC once the first byte is removed and the reserved zero is
        restored.  This is harmless for MaixCAM2 hardware SPI and makes the
        shared recovery logic portable.
        """
        candidates = [raw]
        if raw[:2] != b"\x5a\xa5" and raw[1:3] == b"\x5a\xa5":
            candidates.append(raw[1:] + b"\x00")

        last_error = None
        for candidate in candidates:
            try:
                return p.parse_status(candidate)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise SpiTransactionError(
            f"invalid SPI response: {last_error}") from last_error

    def _transaction_once(self, payload: bytes):
        if self._spi is None:
            raise SpiTransactionError("SPI transport is closed")
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        tx = p.build_frame(p.TYPE_CONTROL, seq, self.session_id, payload)
        rx = self._spi.write_read(tx, len(tx))
        if not isinstance(rx, (bytes, bytearray)) or len(rx) != p.FRAME_SIZE:
            raise SpiTransactionError(
                f"short SPI response: {len(rx) if rx else 0}")
        return self._parse_status_tolerant(bytes(rx))

    def transaction(self):
        with self._lock:
            now = time.monotonic()
            payload = self._payload(now)
            last_error = None
            for attempt in range(self.retries):
                try:
                    status, caps = self._transaction_once(payload)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt + 1 < self.retries:
                        time.sleep(self.retry_delay)
            else:
                raise SpiTransactionError(
                    f"invalid SPI response after {self.retries} "
                    f"attempts: {last_error}") from last_error

            self._last_status = status
            self._last_caps = caps or self._last_caps

            # STATUS is one transaction behind.  Keep HELLO pending until
            # a later STATUS confirms ACTIVE; this recovers from an STM32
            # reset or a 500 ms link timeout.
            self._hello_pending = (status.link_state != p.LINK_ACTIVE)

            if self._pending_event is not None:
                _, event_seq = self._pending_event
                if event_seq < 0:
                    self._pending_event = None
                elif (status.last_event_seq == event_seq or
                      ((status.last_event_seq - event_seq) & 0xFFFFFFFF)
                      < 0x80000000):
                    self._pending_event = None

            return status

    def close(self):
        with self._lock:
            spi = self._spi
            self._spi = None
            close = getattr(spi, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            self._release_bus_lock()
