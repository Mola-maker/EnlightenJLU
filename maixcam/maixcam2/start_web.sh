#!/bin/sh
# MaixCAM2: SenseVoice KWS -> SPI2 owner + local web console.
# The voice process runs with --no-spi so only tools/spi_service.py owns SPI.
set -u

ROOT=/root/JLU_LEDLAB
PIDFILE="$ROOT/run/web.pid"
LOGFILE="$ROOT/logs/web.log"
VOICEFILE="$ROOT/run/voice.pid"
VOICE_ARGS="${WEB_VOICE_ARGS:-}"

mkdir -p "$ROOT/run" "$ROOT/logs"

if [ -f "$VOICEFILE" ]; then
    OLD="$(cat "$VOICEFILE" 2>/dev/null || true)"
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "voice business is already running (PID=$OLD); run ./stop.sh first"
        exit 1
    fi
fi

if [ -f "$PIDFILE" ]; then
    OLD="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "web console already running: PID=$OLD"
        exit 0
    fi
fi

cd "$ROOT" || exit 1
# One process group owns the voice pipeline and the SPI/web service.
setsid sh -c "
  python3 -u app/main.py --no-spi --web-feed $VOICE_ARGS 2>&1 |
  python3 -u tools/spi_service.py --web-port 8080 --frequency 100000
" </dev/null >>"$LOGFILE" 2>&1 &
NEW=$!
echo "$NEW" >"$PIDFILE"
WLAN_IP="$(ip -4 -brief addr show wlan0 2>/dev/null | awk '{print $3}' | cut -d/ -f1)"
echo "MaixCAM2 web console started: PID=$NEW"
echo "URL: http://${WLAN_IP:-<board-ip>}:8080/"
echo "log: $LOGFILE"
