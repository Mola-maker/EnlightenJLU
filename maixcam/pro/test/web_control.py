"""Web control surface for the 13-lamp STM32 test.

The controller is deliberately separated from the SPI transaction loop.  HTTP
threads only update a small desired-state object; the service thread calls
``tick()`` at its normal 20 Hz cadence and performs all SPI writes.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import stm32_protocol as p

LAMP_COUNT = p.LAMP_COUNT
ALL_LAMPS = 0x1FFF
DEFAULT_TARGET = ALL_LAMPS

EFFECT_NAMES = (
    "solid", "breath", "wave", "chase", "sparkle",
)
FIRMWARE_ACTIONS = {
    "standby": p.ACTION_STANDBY,
    "gate": p.ACTION_GATE,
    "release": p.ACTION_RELEASE,
    "egg": p.ACTION_EGG,
    "off": p.ACTION_OFF,
}


def _seq_ge(value, reference):
    return ((value - reference) & 0xFFFFFFFF) < 0x80000000


class LampController:
    def __init__(self, link, hold_seconds=1.2):
        self.link = link
        self.hold_seconds = max(0.1, float(hold_seconds))
        self.lock = threading.RLock()
        self.mode = "kws"
        self.effect = "breath"
        self.target = DEFAULT_TARGET
        self.brightness = 75
        self.duties = [0] * LAMP_COUNT
        self.effect_started = time.monotonic()

        self.gate_until = 0.0
        self.gate_event = None
        self.gate_applied = False
        self.standby_sent = False

        self.firmware_action = None
        self.firmware_sent = False
        self.off_sent = False
        self.last_status = None
        self.message = ""
        self.started = time.monotonic()

    # ---- state transitions ------------------------------------------------
    def snapshot(self):
        with self.lock:
            status = self.link.status
            caps = self.link.caps
            features = int(caps.get("features", 0)) if caps else 0
            direct_ready = bool(features & p.FEATURE_DIRECT)
            return {
                "mode": self.mode,
                "effect": self.effect,
                "target": self.target,
                "brightness": self.brightness,
                "duties": list(self.duties),
                "direct_ready": direct_ready,
                "caps": caps or {},
                "link_state": int(status.link_state) if status else -1,
                "active_mask": int(status.active_mask) if status else 0,
                "glyph_state": list(status.glyph_state) if status else [],
                "fault_flags": int(status.fault_flags) if status else 0,
                "last_control_seq": int(status.last_control_seq) if status else 0,
                "last_event_seq": int(status.last_event_seq) if status else 0,
                "temp_mc": int(status.temp_mc) if status else 0,
                "temp_valid": bool(status.temp_valid) if status else False,
                "message": self.message,
                "uptime_s": round(time.monotonic() - self.started, 1),
                "effects": list(EFFECT_NAMES),
            }

    def command(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        command = str(payload.get("command", "")).strip()
        with self.lock:
            if command == "effect":
                effect = str(payload.get("effect", self.effect))
                if effect not in EFFECT_NAMES:
                    raise ValueError(f"unknown effect: {effect}")
                self.mode = "direct"
                self.effect = effect
                self.target = self._parse_target(payload.get("target",
                                                              self.target))
                self.brightness = self._parse_brightness(
                    payload.get("brightness", self.brightness))
                self.effect_started = time.monotonic()
                self.message = "web effect"
            elif command == "kws":
                self.mode = "kws"
                self.effect = "breath"
                self.target = DEFAULT_TARGET
                self.gate_until = 0.0
                self.gate_event = None
                self.gate_applied = False
                self.standby_sent = False
                self.link.clear_lamps()
                self.link.queue_perform(4, p.ACTION_STANDBY)
                self.standby_sent = True
                self.message = "keyword standby"
            elif command == "off":
                self.mode = "off"
                self.effect = "off"
                self.duties = [0] * LAMP_COUNT
                self.off_sent = False
                self.link.clear_lamps()
                self.message = "all off"
            elif command == "firmware":
                action = str(payload.get("action", "")).strip()
                if action not in FIRMWARE_ACTIONS:
                    raise ValueError(f"unknown firmware action: {action}")
                self.mode = "firmware"
                self.firmware_action = action
                self.firmware_sent = False
                self.link.clear_lamps()
                self.message = f"firmware {action}"
            elif command == "preview":
                # Updates the browser preview without touching the STM32.
                self.effect = str(payload.get("effect", self.effect))
                if self.effect not in EFFECT_NAMES:
                    raise ValueError(f"unknown effect: {self.effect}")
                self.target = self._parse_target(payload.get("target",
                                                              self.target))
                self.brightness = self._parse_brightness(
                    payload.get("brightness", self.brightness))
                if self.mode == "direct":
                    self.effect_started = time.monotonic()
            else:
                raise ValueError(f"unknown command: {command}")
            return self.snapshot()

    def on_kws_trigger(self):
        with self.lock:
            if self.mode != "kws":
                return
            self.gate_until = time.monotonic() + self.hold_seconds
            self.gate_event = None
            self.gate_applied = False
            self.standby_sent = True
            self.message = "keyword gate"

    # ---- main-loop work ---------------------------------------------------
    def tick(self, status):
        now = time.monotonic()
        with self.lock:
            self.last_status = status
            active = status.link_state == p.LINK_ACTIVE
            if not active:
                self.standby_sent = False
                self.gate_event = None
                self.gate_applied = False
                self.firmware_sent = False
                self.off_sent = False

            if self.mode == "kws":
                self._tick_kws(now, status, active)
            elif self.mode == "direct":
                self._tick_direct(now, active)
            elif self.mode == "firmware":
                self._tick_firmware(active)
            elif self.mode == "off":
                self._tick_off(active)

    def _tick_kws(self, now, status, active):
        # Preview the STM32-owned curve: full during GATE, approximate
        # release while the release timer runs, otherwise standby breathing.
        if self.gate_until and now < self.gate_until:
            self.duties = [1000] * LAMP_COUNT
        else:
            phase = (now % 4.0) / 4.0
            curve = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
            level = int(80 * (0.1 + 0.9 * curve))
            self.duties = [level] * LAMP_COUNT
        if not active:
            return
        if self.gate_until and now < self.gate_until:
            if not self.gate_applied and self.gate_event is None:
                self.gate_event = self.link.queue_perform(4, p.ACTION_GATE)
            elif (self.gate_event is not None and
                  _seq_ge(status.last_event_seq, self.gate_event)):
                self.gate_applied = True
                self.gate_event = None
        elif self.gate_until:
            self.link.queue_perform(4, p.ACTION_RELEASE)
            self.gate_until = 0.0
            self.gate_event = None
            self.gate_applied = False
            self.standby_sent = True
            self.message = "keyword release"
        elif not self.standby_sent:
            self.link.queue_perform(4, p.ACTION_STANDBY)
            self.standby_sent = True
            self.message = "keyword standby"

    def _tick_direct(self, now, active):
        self.duties = self._compute_duties(now)
        if not active:
            return
        caps = self.link.caps
        features = int(caps.get("features", 0)) if caps else 0
        if not (features & p.FEATURE_DIRECT):
            return
        self.link.set_lamps(self.duties)

    def _tick_firmware(self, active):
        if not active or self.firmware_sent or not self.firmware_action:
            return
        self.link.clear_lamps()
        action = FIRMWARE_ACTIONS[self.firmware_action]
        self.link.queue_perform(4, action)
        self.firmware_sent = True
        self.message = f"firmware {self.firmware_action}"

    def _tick_off(self, active):
        if not active or self.off_sent:
            return
        self.link.queue_all_off()
        self.off_sent = True
        self.message = "all off"

    # ---- direct-effect math ----------------------------------------------
    def _compute_duties(self, now):
        if self.effect == "off":
            return [0] * LAMP_COUNT
        base = self.brightness * 10
        target = self.target & ALL_LAMPS
        elapsed = now - self.effect_started
        duties = [0] * LAMP_COUNT

        if self.effect == "solid":
            return [base if target & (1 << i) else 0
                    for i in range(LAMP_COUNT)]

        if self.effect == "breath":
            # 4 s sine, 8% floor like the firmware's standby curve.
            phase = (elapsed % 4.0) / 4.0
            curve = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
            level = int(base * (0.08 + 0.92 * curve))
            return [level if target & (1 << i) else 0
                    for i in range(LAMP_COUNT)]

        if self.effect == "wave":
            phase = (elapsed % 3.0) / 3.0
            for i in range(LAMP_COUNT):
                if not (target & (1 << i)):
                    continue
                curve = 0.5 - 0.5 * math.cos(
                    2.0 * math.pi * (phase - i * 0.075))
                duties[i] = int(base * (0.12 + 0.88 * curve))
            return duties

        if self.effect == "chase":
            phase = (elapsed % 2.2) / 2.2
            head = phase * LAMP_COUNT
            for i in range(LAMP_COUNT):
                if not (target & (1 << i)):
                    continue
                distance = abs(i - head)
                if distance > LAMP_COUNT / 2:
                    distance = LAMP_COUNT - distance
                level = max(0.0, 1.0 - distance / 2.7)
                duties[i] = int(base * (0.08 + 0.92 * level))
            return duties

        # sparkle: smooth, deterministic, no random flicker.
        for i in range(LAMP_COUNT):
            if not (target & (1 << i)):
                continue
            x = math.sin(elapsed * 3.1 + i * 1.37)
            y = math.sin(elapsed * 1.7 + i * 0.61)
            level = max(0.0, (x * 0.6 + y * 0.4)) ** 2
            duties[i] = int(base * (0.05 + 0.95 * level))
        return duties

    @staticmethod
    def _parse_brightness(value):
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError("brightness must be 0..100") from exc
        if not 0 <= value <= 100:
            raise ValueError("brightness must be 0..100")
        return value

    @staticmethod
    def _parse_target(value):
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("target must be a 13-bit mask") from exc
        if value < 0 or value > ALL_LAMPS:
            raise ValueError("target must be a 13-bit mask")
        return value


def create_app(controller, web_dir: Path, port: int | None = None):
    from flask import Flask, Response, jsonify, request, send_from_directory

    app = Flask(__name__, static_folder=str(web_dir), static_url_path="/static")
    app.config["WEB_PORT"] = port

    @app.after_request
    def no_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/api/state")
    def state():
        return jsonify(controller.snapshot())

    @app.post("/api/command")
    def command():
        payload = request.get_json(silent=True)
        try:
            return jsonify(controller.command(payload or {}))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/stream")
    def stream():
        def events():
            while True:
                yield "data: " + json.dumps(
                    controller.snapshot(), separators=(",", ":")) + "\n\n"
                time.sleep(0.1)

        return Response(events(), mimetype="text/event-stream")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "port": app.config.get("WEB_PORT")})

    return app
