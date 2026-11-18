"""Flask surface for the focused local lamp/thermal console."""
from __future__ import annotations

import json
import time
from pathlib import Path


def create_console_app(state_fn, history_fn, lamp, thermal, web_dir: Path,
                       port: int, wifi=None):
    from flask import Flask, Response, jsonify, request, send_from_directory

    app = Flask(__name__, static_folder=str(web_dir), static_url_path="/static")
    app.config["WEB_PORT"] = port

    @app.after_request
    def no_cache(response):
        if request.path.startswith(("/api/", "/static/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/api/state")
    def state():
        return jsonify(state_fn())

    @app.get("/api/history")
    def history():
        return jsonify(history_fn())

    @app.post("/api/lamp")
    def lamp_command():
        payload = request.get_json(silent=True) or {}
        try:
            command = str(payload.get("command", "effect"))
            if command == "kws":
                result = lamp.command({"command": "kws"})
            elif command == "off":
                result = lamp.command({"command": "off"})
            elif command == "effect":
                result = lamp.command({
                    "command": "effect",
                    "effect": str(payload.get("effect", "breath")),
                    "target": int(payload.get("target", 0x1FFF)),
                    "brightness": float(payload.get("brightness", 75)),
                })
            else:
                raise ValueError(f"unknown lamp command: {command}")
            return jsonify(state_fn())
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/thermal")
    def thermal_state():
        """Derived tuning (feedforward duty + gains) and limits."""
        return jsonify(state_fn()["thermal"])

    @app.post("/api/thermal")
    def thermal_command():
        """Configure the heater from physical inputs.

        Expected payload: ``ambient_c`` and ``no_heat_junction_c``, optionally
        ``setpoint_c`` / ``margin_c`` / ``max_duty`` / ``enabled``.  The
        feedforward duty, warm-up band and PI gains are derived on the device;
        ``kp`` / ``ki`` / ``kd`` are ignored unless ``engineering_override`` is
        set explicitly (bench bring-up only).
        """
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(thermal.configure(payload))
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/wifi")
    def wifi_state():
        """Current association plus the credentials that are configured."""
        if wifi is None:
            return jsonify({"error": "wifi control unavailable"}), 501
        return jsonify(wifi.read_state())

    @app.post("/api/wifi")
    def wifi_command():
        """Overwrite the board's WiFi credentials and reconnect.

        Body: ``{"ssid": "...", "password": "..."}`` (empty password = open
        network).  The reply is sent before the link is reloaded, so a client
        on the old network sees the 202 and then loses the connection on
        purpose -- reconnect to the new network to reach the board again.
        """
        if wifi is None:
            return jsonify({"error": "wifi control unavailable"}), 501
        payload = request.get_json(silent=True) or {}
        try:
            state = wifi.write(str(payload.get("ssid", "")),
                               str(payload.get("password", "")))
        except (TypeError, ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        wifi.apply_async()
        return jsonify({
            "accepted": True,
            "message": "已写入，设备将在几秒内重连新网络",
            "wifi": state,
        }), 202

    @app.get("/api/stream")
    def stream():
        def events():
            while True:
                yield "data: " + json.dumps(
                    state_fn(), separators=(",", ":")) + "\n\n"
                time.sleep(0.2)

        return Response(events(), mimetype="text/event-stream")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "port": port})

    return app
