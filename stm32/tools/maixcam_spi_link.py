"""Minimal MaixCAM SPI master link for the STM32 PWM-Heater board.

This is a transport/reference module.  On MaixCAM Pro the tested
raw-spidev path is maixcam/pro/test/spi_link.py (software SPI4, /dev/spidev4.0);
this module uses the MaixPy hardware SPI API and is primarily used on MaixCAM2.

Typical use from a lightweight link process:

    link = Stm32SpiLink()
    link.set_temp(temp_mc, sample_seq, age_ms, valid=True)
    link.queue_perform(glyph_mask=0x3, action=ACTION_GATE)
    while running:
        status = link.transaction()
        ...
        time.sleep(0.05)

The STM32 parses a request after the SPI transaction, so its STATUS response
is observed on the next transaction.  Pending PERFORM events are resent until
STATUS.last_event_seq acknowledges them.
"""

from __future__ import annotations

import os
import threading
import time

import stm32_protocol as p


class Stm32SpiLink:
    def __init__(self, spi_id: int | None = None,
                 frequency: int = 500_000,
                 session_id: int | None = None,
                 retries: int = 6,
                 retry_delay: float = 0.05):
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

        for pin, function in pins.items():
            err.check_raise(pinmap.set_pin_function(pin, function),
                            f"pinmap {pin} -> {function} failed")

        self._spi = spi.SPI(default_spi_id if spi_id is None else spi_id,
                            spi.Mode.MASTER, frequency,
                            polarity=0, phase=0, bits=8)
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
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))

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
        self._event_seq += 1
        event = p.perform(glyph_mask, action, event_seq, profile_id, value)
        with self._lock:
            self._pending_event = (event, event_seq)
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
            # In practice HELLO/TEMP/PERFORM/HB are small enough.  Drop the
            # optional temperature chunk before giving up.
            payload = b"".join(chunk for chunk in chunks
                               if chunk is not self._temp)
        if len(payload) > p.MAX_PAYLOAD:
            raise RuntimeError("SPI v1 payload exceeds 44 bytes")
        return payload

    def _transaction_once(self, payload):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        tx = p.build_frame(p.TYPE_CONTROL, seq, self.session_id, payload)
        rx = bytes(self._spi.write_read(tx, len(tx)))
        if len(rx) != p.FRAME_SIZE:
            raise RuntimeError(f"short SPI response: {len(rx)}")
        return p.parse_status(rx)

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
                raise RuntimeError(
                    f"invalid SPI response after {self.retries} "
                    f"attempts: {last_error}") from last_error

            self._last_status = status
            self._last_caps = caps

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
        close = getattr(self._spi, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
