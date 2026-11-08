"""SPI master transport for the STM32 PWM-Heater test firmware.

MaixCAM Pro SPI4: A22 SCK, A23 MISO, A24 CS, A25 MOSI.
Linux device: /dev/spidev4.0 (software SPI4 over spi4@gpio).

The SPI4 bit-banged controller is not the hardware SPI3 device at
/dev/spidev3.0.  Its spidev driver also rejects an explicit ``mode = 0``
write and always reports the CS_HIGH flag, so this transport leaves the
mode at its default value.  Reads are validated by magic/length/CRC and
retried because the software controller can occasionally return a
truncated or shifted response under system load.
"""
from __future__ import annotations

import os
import threading
import time

import spidev
import stm32_protocol as p
from spi4_bootstrap import ensure_spi4_device, parse_spi4_response


class SpiTransactionError(RuntimeError):
    """No valid STATUS frame was received after all retries."""


class Stm32SpiLink:
    def __init__(self, bus=4, device=0, frequency=500_000,
                 session_id=None, retries=5, retry_delay=0.1,
                 settle_time=0.5):
        self.bus = int(bus)
        self.device = int(device)
        self.frequency = int(frequency)
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.settle_time = max(0.0, float(settle_time))

        if self.bus == 4:
            ensure_spi4_device()

        self._spi = spidev.SpiDev()
        self._spi.open(self.bus, self.device)
        self._spi.bits_per_word = 8
        self._spi.max_speed_hz = self.frequency
        if self.bus == 4 and self.settle_time:
            # The bit-banged SPI4 controller needs a short settling
            # period after open; early transfers otherwise return
            # truncated/0xFF frames under load.
            time.sleep(self.settle_time)
        # Do not write self._spi.mode / cshigh here.  The spi-gpio
        # driver used by SPI4 rejects mode=0 and reports mode=0x4.

        self.session_id = session_id or (
            int.from_bytes(os.urandom(4), "little") or 1
        )
        self.seq = 1
        self._event_seq = 1
        self._hello_pending = True
        self._pending_event = None
        self._direct_duties = None
        self._temp = None
        self._temp_sent_at = 0.0
        self._last_status = None
        self._last_caps = None
        self._started = time.monotonic()
        self._lock = threading.Lock()

    @property
    def status(self):
        return self._last_status

    @property
    def caps(self):
        return self._last_caps

    def set_temp(self, temp_mc, sample_seq, age_ms=0, valid=True, source=0):
        with self._lock:
            self._temp = p.temp(temp_mc, sample_seq, age_ms, valid, source)

    def set_lamps(self, duties):
        if len(duties) != p.LAMP_COUNT:
            raise ValueError(f"expected {p.LAMP_COUNT} lamp duties")
        with self._lock:
            self._direct_duties = [
                max(0, min(1000, int(duty))) for duty in duties
            ]
            self._pending_event = None

    def clear_lamps(self):
        with self._lock:
            self._direct_duties = None

    def queue_perform(self, glyph_mask, action, profile_id=0, value=0):
        with self._lock:
            self._direct_duties = None
            event_seq = self._event_seq
            self._event_seq = (self._event_seq + 1) & 0xFFFFFFFF
            self._pending_event = (
                p.perform(glyph_mask, action, event_seq, profile_id, value),
                event_seq,
            )
            return event_seq

    def queue_all_off(self):
        with self._lock:
            self._direct_duties = None
            self._pending_event = (p.all_off(), -1)

    def _payload(self, now):
        # Caller holds self._lock.
        chunks = []
        if self._hello_pending:
            chunks.append(p.hello(
                heartbeat_ms=50,
                features=(p.FEATURE_PERFORM | p.FEATURE_TEMP |
                          p.FEATURE_HEARTBEAT),
            ))
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
                state_flags=0))

        payload = b"".join(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            # Drop the optional temperature chunk before giving up.
            payload = b"".join(
                chunk for chunk in chunks if chunk is not self._temp)
        if len(payload) > p.MAX_PAYLOAD:
            raise RuntimeError("SPI payload exceeds 44 bytes")
        return payload

    def _transaction_once(self, payload):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        tx = p.build_frame(p.TYPE_CONTROL, seq, self.session_id, payload)
        rx = self._spi.xfer2(list(tx))
        try:
            raw = bytes(rx)
        except (TypeError, ValueError) as exc:
            raise SpiTransactionError(
                "SPI driver returned a non-byte response") from exc
        if len(raw) != p.FRAME_SIZE:
            raise SpiTransactionError(
                f"short SPI response: {len(raw)} bytes")
        return parse_spi4_response(raw)

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
                    f"invalid SPI response after {self.retries} attempts: "
                    f"{last_error}") from last_error

            self._last_status = status
            self._last_caps = caps

            # STATUS is one transaction behind.  If the STM32 reports
            # BOOT/WAIT_HELLO/LOST/FAULT, keep sending HELLO until a
            # later STATUS confirms ACTIVE.  This also recovers from an
            # STM32 reset or a >500 ms link timeout without restarting
            # the MaixCAM process.
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
        try:
            self._spi.close()
        except Exception:  # noqa: BLE001
            pass
