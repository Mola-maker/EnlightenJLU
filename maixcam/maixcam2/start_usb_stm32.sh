#!/bin/sh
# USB microphone + STM32 SPI2/CS1 voice business.
#
# This is the hardware path: SenseVoice recognizes Chinese "长春", the app
# sends PERFORM GATE/RELEASE to the STM32 over SPI2/CS1, and the STM32 drives
# PWM1..PWM13.  It intentionally does not use the MaixCAM Pro test's
# English-proxy keyword set; MaixCAM2 uses its own SenseVoice model and
# Chinese keyword configuration.
set -u

ROOT=/root/JLU_LEDLAB
cd "$ROOT" || exit 1

if ! arecord -l 2>/dev/null | grep -qi 'USB Audio'; then
    if [ -x "$ROOT/usb_audio/enable_usb_mic.sh" ]; then
        "$ROOT/usb_audio/enable_usb_mic.sh" || exit 1
    else
        echo "USB Audio is not available and enable_usb_mic.sh is missing" >&2
        exit 1
    fi
fi

exec "$ROOT/start.sh" --usb-mic "$@"
