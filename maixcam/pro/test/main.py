#!/usr/bin/env python3
"""SPI + English-keyword smoke test for MaixCAM Pro.

The installed Maix-Speech KWS model is a Mandarin pinyin model.  Direct English
keywords are rejected by its parser, so this test registers phonetic proxies:
    hello   -> ha1 lou2       (哈喽)
    welcome -> wei1 er2 kang1 (威尔康)

A detected proxy sends PERFORM to the STM32 with glyph_mask=0x4
(GLYPH_ALL_LAMPS), which drives PWM1..PWM13 and excludes HEAT1/HEAT2.
Temperature is read from SG2002 sysfs once per second and sent in TEMP TLVs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = "/root/surplife/app"
for path in (APP_DIR, TEST_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import stm32_protocol as proto
from spi4_bootstrap import configure_spi4_pins, ensure_spi4_device
from spi_link import Stm32SpiLink
from sg2002_temp import sample as read_temperature

from maix import app, err, nn

log = logging.getLogger("JLU_LEDLAB.test")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def configure_mic_gain(cfg):
    """Apply the USB microphone capture gain before opening ALSA.

    The PCM2902 exposes a 0..16 capture volume; leaving it at 0 can make
    live KWS unreliable.  Failures are non-fatal so WAV-only tests still run.
    """
    device = str(cfg.get("alsa_device", ""))
    match = re.search(r"(?:plughw|hw):(\d+)", device)
    if not match:
        return
    card = match.group(1)
    gain = cfg.get("mic_gain")
    if gain is not None:
        result = subprocess.run(
            ["amixer", "-c", card, "sset", "Mic", str(int(gain))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            log.warning("failed to set USB mic gain on card %s", card)
        else:
            log.info("USB mic card %s capture gain=%s", card, gain)
    if "mic_agc" in cfg:
        value = "on" if cfg["mic_agc"] else "off"
        subprocess.run(
            ["amixer", "-c", card, "sset", "Auto Gain Control", value],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TempWorker(threading.Thread):
    def __init__(self, link, cfg, stop_event):
        super().__init__(name="temp", daemon=True)
        self.link = link
        self.cfg = cfg
        self.stop = stop_event
        self.seq = 0

    def run(self):
        tc = self.cfg.get("temperature", {})
        interval = float(tc.get("interval_seconds", 1.0))
        source = tc.get("source", "sysfs")
        while not self.stop.is_set():
            try:
                item = read_temperature(source)
                self.seq = (self.seq + 1) & 0xFFFF
                if self.link is not None:
                    self.link.set_temp(item["temp_mc"], self.seq, 0, True,
                                      0 if item.get("source") == "sysfs" else 1)
                log.info("TEMP %.3f C -> STM32 seq=%d", item["temp_c"], self.seq)
            except Exception as exc:  # noqa: BLE001
                log.warning("temperature failed: %s", exc)
            self.stop.wait(interval)


class SpiWorker(threading.Thread):
    def __init__(self, link, cfg, stop_event):
        super().__init__(name="spi", daemon=True)
        self.link = link
        self.cfg = cfg
        self.stop = stop_event
        self.was_active = False
        self.standby_sent = False
        self.fails = 0

    def run(self):
        while not self.stop.is_set():
            try:
                status = self.link.transaction()
                self.fails = 0
                active = status.link_state == proto.LINK_ACTIVE
                if active != self.was_active:
                    log.info("STM32 link %s state=%d faults=0x%04X",
                             "ACTIVE" if active else "not active",
                             status.link_state, status.fault_flags)
                    self.was_active = active
                if not active:
                    # The STM32 has forced all outputs off and requires a
                    # fresh HELLO.  Re-arm standby after the link recovers.
                    self.standby_sent = False
                if active and not self.standby_sent:
                    self.standby_sent = True
                    if self.cfg.get("standby_on_start", False):
                        seq = self.link.queue_perform(
                            int(self.cfg["all_lamps_mask"]), proto.ACTION_STANDBY)
                        log.info("PERFORM STANDBY all-lamps event=%d", seq)
            except Exception as exc:  # noqa: BLE001
                self.fails += 1
                if self.fails in (1, 10, 100):
                    log.warning("SPI failed (%d): %s", self.fails, exc)
            self.stop.wait(0.05)


class KeywordEngine:
    def __init__(self, cfg, link, debug_scores=False):
        self.cfg = cfg
        self.link = link
        self.debug_scores = debug_scores
        self.stop = threading.Event()
        self.release_timer = None
        self.release_lock = threading.Lock()
        self.last_fire = {}
        self.keywords = [x["kws"] for x in cfg["keywords"]]
        self.labels = [x["label"] for x in cfg["keywords"]]
        self.display = [x.get("display", x["label"]) for x in cfg["keywords"]]
        self.thresholds = [float(x.get("threshold", 0.1)) for x in cfg["keywords"]]
        self.cooldowns = [float(x.get("cooldown", 2.0)) for x in cfg["keywords"]]
        self.speech = None

    def on_trigger(self, index, probability):
        now = time.monotonic()
        if now - self.last_fire.get(index, -1e9) < self.cooldowns[index]:
            return
        self.last_fire[index] = now
        mask = int(self.cfg.get("all_lamps_mask", 4))
        log.info("TRIGGER %s (%s) p=%.3f -> GATE mask=0x%X",
                 self.labels[index], self.display[index], probability, mask)
        if self.link is not None:
            seq = self.link.queue_perform(mask, proto.ACTION_GATE)
            log.info("PERFORM GATE all-lamps event=%d", seq)
        with self.release_lock:
            if self.release_timer is not None:
                self.release_timer.cancel()
            self.release_timer = threading.Timer(
                float(self.cfg.get("hold_seconds", 1.2)),
                self._release, args=(mask,))
            self.release_timer.daemon = True
            self.release_timer.start()

    def _release(self, mask):
        if self.stop.is_set():
            return
        if self.link is not None:
            seq = self.link.queue_perform(mask, proto.ACTION_RELEASE)
            log.info("PERFORM RELEASE all-lamps event=%d", seq)

    def _callback(self, data, length):
        probs = []
        for i in range(min(length, len(self.keywords))):
            try:
                probs.append(float(data[i]))
            except (TypeError, ValueError):
                probs.append(0.0)
        if self.debug_scores and probs:
            log.info("KWS scores %s",
                     " ".join(f"{self.labels[i]}={p:.3f}"
                              for i, p in enumerate(probs)))
        for i, prob in enumerate(probs):
            if prob >= self.thresholds[i]:
                self.on_trigger(i, prob)

    def start(self, wav):
        self.speech = nn.Speech(self.cfg["model"])
        if wav:
            ret = self.speech.init(nn.SpeechDevice.DEVICE_WAV, wav)
            log.info("KWS WAV input: %s", wav)
        else:
            ret = self.speech.init(nn.SpeechDevice.DEVICE_MIC,
                                   self.cfg.get("alsa_device", "surplife_mic"))
            log.info("KWS microphone: %s", self.cfg.get("alsa_device"))
        if ret != err.Err.ERR_NONE:
            raise RuntimeError(f"speech init failed: {ret}")
        ret = self.speech.kws(list(self.keywords), list(self.thresholds),
                              self._callback, True)
        if ret != err.Err.ERR_NONE:
            raise RuntimeError(f"kws init failed: {ret}")
        log.info("keywords: %s", ", ".join(
            f"{d} -> {k}" for d, k in zip(self.display, self.keywords)))
        log.info("all-lamp mask 0x%X = PWM1..PWM13, HEAT excluded",
                 int(self.cfg.get("all_lamps_mask", 4)))

    def stop_engine(self):
        self.stop.set()
        with self.release_lock:
            if self.release_timer is not None:
                self.release_timer.cancel()
                self.release_timer = None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=os.path.join(TEST_DIR, "config.json"))
    p.add_argument("--wav", default=None)
    p.add_argument("--no-spi", action="store_true")
    p.add_argument("--debug-kws", action="store_true",
                   help="log every keyword probability frame")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    configure_mic_gain(cfg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def open_spi_link():
        bus = int(cfg.get("spi_bus", 4))
        frequency = int(cfg.get("spi_frequency", 500_000))
        retries = int(cfg.get("spi_retries", 5))
        retry_delay = max(
            0.0, float(cfg.get("spi_retry_delay_ms", 100)) / 1000.0)
        settle_time = max(
            0.0, float(cfg.get("spi4_settle_ms", 500)) / 1000.0)
        if bus == 4:
            ensure_spi4_device()
            configure_spi4_pins()
        link = Stm32SpiLink(
            bus=bus,
            device=int(cfg.get("spi_device", 0)),
            frequency=frequency,
            retries=retries,
            retry_delay=retry_delay,
            settle_time=settle_time)
        log.info("SPI /dev/spidev%d.%d @ %d Hz, retries=%d/%dms",
                 bus, int(cfg.get("spi_device", 0)),
                 frequency, retries, int(retry_delay * 1000))
        return link

    link = None
    spi_worker = temp_worker = None
    engine = KeywordEngine(cfg, None, debug_scores=args.debug_kws)
    try:
        # Load KWS and configure USB audio before opening the bit-banged
        # SPI4 device.  KWS/audio initialization can disturb the GPIO
        # state if the spidev fd is already open.
        engine.start(args.wav)
        if not args.no_spi:
            try:
                link = open_spi_link()
            except Exception as exc:  # noqa: BLE001
                log.error("SPI open failed: %s", exc)
        engine.link = link
        if link is not None:
            spi_worker = SpiWorker(link, cfg, stop)
            temp_worker = TempWorker(link, cfg, stop)
            spi_worker.start()
            temp_worker.start()
        else:
            log.warning("SPI disabled; keyword events are log-only")
        while not stop.is_set() and not app.need_exit():
            n = engine.speech.run(1)
            if n < 1:
                log.info("audio input finished")
                break
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("fatal: %s", exc)
        return 1
    finally:
        engine.stop_engine()
        # Let the SPI worker send ALL_OFF before stopping its loop.
        if link is not None:
            try:
                link.queue_all_off()
                time.sleep(0.2)
            except Exception:  # noqa: BLE001
                pass
        stop.set()
        if link is not None:
            link.close()
        if spi_worker is not None:
            spi_worker.join(1.0)
        if temp_worker is not None:
            temp_worker.join(1.0)
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
