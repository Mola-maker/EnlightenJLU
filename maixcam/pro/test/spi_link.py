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

import fcntl
import os
import threading
import time

import spidev
import stm32_protocol as p
from spi4_bootstrap import ensure_spi4_device, parse_spi4_response


class SpiTransactionError(RuntimeError):
    """No valid STATUS frame was received after all retries."""


class Stm32SpiLink:
    def __init__(self, bus=4, device=0, frequency=100_000,
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

        self._bus_lock_fd = None
        self._acquire_bus_lock()
        self._spi = None
        try:
            self._open_transport()
        except Exception:
            self._release_bus_lock()
            raise

        self.session_id = session_id or (
            int.from_bytes(os.urandom(4), "little") or 1
        )
        self.seq = 1
        self._event_seq = 1
        self._hello_pending = True
        self._pending_event = None
        self._direct_duties = None
        self._thermal_config = None
        self._thermal_config_saved = None
        self._thermal_config_enable = False
        self._thermal_split_heat = False
        self._thermal_config_pending = False
        self._heater = None
        self._temp = None
        self._temp_sent_at = 0.0
        self._last_status = None
        self._last_caps = None
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def _acquire_bus_lock(self):
        """Prevent two MaixCAM processes from clocking the same STM32 bus.

        The web service and the command-line thermal controller both open
        `/dev/spidev4.0`.  Without an inter-process lock, a leftover S99
        supervisor or a second manual run can interleave frames: each process
        presents a different session_id, so the STM32 resets its thermal
        envelope and both sides see random `bad magic` responses.
        """
        path = f"/tmp/stm32-spi{self.bus}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"SPI bus {self.bus} is already owned by another process "
                f"({path})") from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        self._bus_lock_fd = fd

    def _release_bus_lock(self):
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

    def _open_transport(self):
        """Open /dev/spidevX.Y and apply the settings this bus requires."""
        spi = spidev.SpiDev()
        spi.open(self.bus, self.device)
        spi.bits_per_word = 8
        spi.max_speed_hz = self.frequency
        if self.bus == 4 and self.settle_time:
            # The bit-banged SPI4 controller needs a short settling
            # period after open; early transfers otherwise return
            # truncated/0xFF frames under load.
            time.sleep(self.settle_time)
        # Do not write spi.mode / cshigh here.  The spi-gpio driver used by
        # SPI4 rejects mode=0 and reports mode=0x4.
        self._spi = spi

    def reopen(self):
        """Recover a wedged software-SPI transport.

        A spidev file descriptor can survive individual bad frames but stop
        producing valid ones after a reset or a long error burst.  Reopening
        the descriptor and re-arming HELLO + THERMAL_CONFIG is the only way
        to get out of that state without restarting the controller process.
        """
        with self._lock:
            old_spi = self._spi
            self._spi = None
            if old_spi is not None:
                try:
                    old_spi.close()
                except Exception:  # noqa: BLE001
                    pass

            self._open_transport()
            self._hello_pending = True
            self._thermal_config = self._thermal_config_saved
            self._thermal_config_pending = False
            self._temp_sent_at = 0.0
            self._last_status = None
            self._last_caps = None

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

    def set_thermal_config(self, setpoint_mc, margin_mc, kp_x100=6000,
                           ki_x100=200, kd_x100=0, max_duty=300,
                           enable=True, split_heat=False):
        with self._lock:
            config = p.thermal_config(
                setpoint_mc, margin_mc, kp_x100, ki_x100, kd_x100,
                max_duty, enable, split_heat)
            self._thermal_config = config
            self._thermal_config_saved = config
            self._thermal_config_enable = bool(enable)
            self._thermal_split_heat = bool(split_heat)

    def set_heater(self, duty, enable=True):
        with self._lock:
            self._heater = p.heater(duty, enable)

    def clear_heater(self):
        with self._lock:
            self._heater = None

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
            self._heater = None
            self._pending_event = (p.all_off(), -1)

    def _payload(self, now):
        # Caller holds self._lock.
        chunks = []

        def add(kind, value):
            chunks.append((kind, value))

        if self._hello_pending:
            add("hello", p.hello(
                heartbeat_ms=50,
                features=(p.FEATURE_PERFORM | p.FEATURE_TEMP |
                          p.FEATURE_HEARTBEAT | p.FEATURE_THERMAL_PID),
            ))
        elif self._temp is not None and (now - self._temp_sent_at) >= 1.0:
            add("temp", self._temp)
            self._temp_sent_at = now

        thermal_active = (self._thermal_config is not None or
                          self._heater is not None)
        if self._pending_event is not None:
            add("event", self._pending_event[0])
        elif self._direct_duties is not None:
            add("direct", p.set_lamps(self._direct_duties))
        elif not self._hello_pending and not thermal_active:
            add("heartbeat", p.heartbeat(
                maix_uptime_ms=int((now - self._started) * 1000),
                state_flags=0))

        # Append the safety configuration even when HELLO is being sent.  The
        # STM32 handles HELLO first and then THERMAL_CONFIG in the same frame,
        # so a reset/link-timeout cannot strand the heater in the
        # "active but unconfigured" state.  Previously these two chunks were
        # mutually exclusive, which made recovery from a marginal link loop
        # forever between WAIT_HELLO and ACTIVE with output permanently off.
        if self._thermal_config is not None:
            add("thermal_config", self._thermal_config)
            self._thermal_config_pending = True
        if self._heater is not None:
            add("heater", self._heater)

        def pack(items):
            return b"".join(value for _, value in items)

        payload = pack(chunks)
        # TRANSPORT BUDGET: 44 payload bytes.  Never silently drop a safety
        # chunk.  Drop the optional temp first, then defer the lamp frame by
        # one transaction so THERMAL_CONFIG + HEATER can fit alongside it.
        if len(payload) > p.MAX_PAYLOAD:
            chunks = [item for item in chunks if item[0] != "temp"]
            payload = pack(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            chunks = [item for item in chunks if item[0] != "direct"]
            payload = pack(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            chunks = [item for item in chunks if item[0] != "heartbeat"]
            payload = pack(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            # A queued lamp event is not a safety chunk.  Defer it for one
            # transaction rather than failing the whole SPI keepalive frame.
            chunks = [item for item in chunks if item[0] != "event"]
            payload = pack(chunks)
        if len(payload) > p.MAX_PAYLOAD:
            raise RuntimeError("SPI payload exceeds 44 bytes")
        return payload

    def _transaction_once(self, payload):
        if self._spi is None:
            raise SpiTransactionError("SPI transport is closed")
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
            self._last_caps = caps or self._last_caps
            if self._thermal_config_pending:
                # Send THERMAL_CONFIG once, not on every keepalive frame.
                self._thermal_config = None
                self._thermal_config_pending = False

            # STATUS is one transaction behind.  If the STM32 reports
            # BOOT/WAIT_HELLO/LOST/FAULT, keep sending HELLO until a
            # later STATUS confirms ACTIVE.  This also recovers from an
            # STM32 reset or a >500 ms link timeout without restarting
            # the MaixCAM process.
            self._hello_pending = (status.link_state != p.LINK_ACTIVE)

            if self._thermal_config_saved is not None:
                caps_now = self._last_caps or {}
                supports_thermal = bool(
                    caps_now.get("features", 0) & p.FEATURE_THERMAL_PID)
                configured = bool(status.heater_flags & 0x10)
                enabled = bool(status.heater_flags & 0x01)
                need_config = (not configured or
                               (self._thermal_config_enable and not enabled))
                if (self._hello_pending or
                        (supports_thermal and need_config)):
                    # Re-arm the safety envelope together with HELLO.  If
                    # the status is one frame behind, the config is sent
                    # again and is harmless; it stops as soon as STATUS
                    # reports configured (and enabled when requested).
                    self._thermal_config = self._thermal_config_saved
                    self._temp_sent_at = 0.0

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
            if spi is not None:
                try:
                    spi.close()
                except Exception:  # noqa: BLE001
                    pass
            self._release_bus_lock()
