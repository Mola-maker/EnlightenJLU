> **仓库位置**：`maixcam/maixcam2/`；板端部署路径仍为 `/root/JLU_LEDLAB/`。模型描述和支持代码见 `model/maixcam2/`，执行器见 `stm32/`。

# JLU_LEDLAB MaixCAM2 application

Runtime location: `/root/JLU_LEDLAB`

MaixCAM2 只负责音频采集、SenseVoice 语音识别和高层业务判断；**所有 PWM
波形、待机呼吸、GATE/HOLD/RELEASE/EGG 状态机以及 HEAT 安全包络都由 STM32
产生**。MaixCAM2 不配置任何板载 PWM 引脚，只通过 SPI2/CS1 发送
`PERFORM`、`TEMP`、`HEARTBEAT` 等高层命令。

The SenseVoice model is **not copied** into this directory. The application
uses the system model directory:

```text
/root/models/sensevoice-maixcam2/
```

`config/sensevoice.mud` is a small local descriptor that points at the external
`sensevoice.axmodel`, `streaming_sensevoice.axmodel`, and `server.py`.

## Directory layout

```text
/root/JLU_LEDLAB/
  app/
    main.py                 voice recognition + STM32 business
    keyword_matcher.py      Chinese character-family matching
    stm32_protocol.py       SPI v1 frame/TLV implementation
    maixcam_spi_link.py     MaixCAM2 SPI2/CS1 master transport
    test_keyword_matcher.py
  config/
    sensevoice.mud          external-model descriptor
    keywords.json           registered phrase/character families
  tools/
    spi_service.py          SPI owner + Flask web console
    web_control.py          lamp/direct-effect controller
    thermal_control.py      heater PI/thermal state
    wifi_info.py            read-only WiFi status
    S99jluweb              boot hook
  web/
    index.html              local console page
    app.js / style.css
  logs/
  run/
  run_voice.sh
  start.sh
  start_usb_stm32.sh
  start_web.sh
  stop_web.sh
  stop.sh
```

There is no `local_pwm.py`, no `--local-pwm` option and no `start_usb_local.sh`
in this application.

## Registered vocabulary

| Legacy pinyin | SenseVoice family | Chinese aliases |
|---|---|---|
| chang1 / chang2 / chang4 | chang | 长 常 场 厂 唱 昌 畅 尝 肠 偿 敞 倡 |
| chun1 / chun2 / chun3 | chun | 春 纯 唇 醇 淳 蠢 椿 鹑 |
| ang1 / ang2 | ang | 昂 肮 盎 |
| chang2 chun1 | phrase | 长春 / 长 春 |

SenseVoice returns Chinese text without tone labels, so tone variants are kept
as registered metadata but matching is done by the character family. `ang` is
debug-only and does not open the gate. A single `chang` or `chun` is logged but
does not gate; `长春` or `chang + chun` opens the gate.

## Run

Built-in microphone + STM32:

```sh
cd /root/JLU_LEDLAB
./run_voice.sh
```

USB microphone + STM32:

```sh
cd /root/JLU_LEDLAB
./start_usb_stm32.sh
```

`start_usb_stm32.sh` checks/enables USB Audio, then calls
`./start.sh --usb-mic` without any local-PWM option. The app opens the STM32
SPI link and sends real `PERFORM` events.

Voice-only debugging:

```sh
./run_voice.sh --no-spi
./run_voice.sh --wav /root/JLU_LEDLAB/bench/audio/changchun.wav --no-spi
./run_voice.sh --list-keywords
```

Background management:

```sh
./start_usb_stm32.sh
./stop.sh
tail -f logs/voice.log
```

## Web console

MaixCAM2 和 Pro 一样有本地中控台；它由 `tools/spi_service.py` 持有 SPI2
总线，语音进程以 `--no-spi --web-feed` 运行，只把 `KWS READY/TRIGGER`
喂给中控台，不直接碰 PWM。

```sh
cd /root/JLU_LEDLAB
./start_web.sh
./stop_web.sh
tail -f logs/web.log
```

地址：

```text
http://192.168.1.190:8080/
```

MaixCAM2 的 nginx 占用 80 端口，因此本中控台固定在 **8080**；服务绑定
`0.0.0.0:8080`。页面包含：

- 13 路灯牌波形、网页波形/整体亮度控制；
- 关键词状态（SenseVoice 识别 `长春` 后触发 GATE/RELEASE）；
- STM32 温度、HEAT duty、温控曲线；
- 当前 WiFi SSID / IP（MaixCAM2 上只读显示，不在网页里改写
  `/etc/wpa_supplicant.conf`）。

网页的 KWS 待机/触发模式沿用 Pro 中控台，使用 `mask=0x4`（PWM1–PWM13
全灯）；单独运行 `start_usb_stm32.sh` 时仍使用业务配置的 `mask=0x3`
（长/春）。两者都由 STM32 执行。

温控页的默认整定参数继承自 Pro 的 SG2002 热模型；MaixCAM2 是 AX620E，
首次使用加热前应重新标定环境温度/无加热结温。加热默认关闭。

`S99jluweb` 已安装到 `/etc/init.d/S99jluweb`，开机自动启动中控台。手动：

```sh
/etc/init.d/S99jluweb start|status|stop|restart
```

**不要同时运行**网页中控台和 `start_usb_stm32.sh` / `run_voice.sh`：两者
都会打开 SPI2 总线；中控台已经持有 `/tmp/stm32-spi2.lock`。先
`./stop_web.sh`，再单独运行语音业务；反之亦然。

`start_web.sh` 默认使用板载麦克风。USB 麦克风可以用：

```sh
WEB_VOICE_ARGS="--usb-mic" ./start_web.sh
```

## STM32 link

MaixCAM2 uses hardware SPI2/CS1:

| MaixCAM2 | STM32 |
|---|---|
| B20 / SPI2_SCK | PA5 / SPI1_SCK |
| B19 / SPI2_MISO | PA6 / SPI1_MISO |
| B21 / SPI2_CS1 | PA4 / SPI1_NSS（软件 NSS 可保留） |
| B18 / SPI2_MOSI | PA7 / SPI1_MOSI |
| GND | GND |

Linux device: `/dev/spidev2.1`.

The STM32 receives `HELLO`, `TEMP`, `PERFORM`, and `HEARTBEAT` according to
`stm32/docs/SPI_PROTOCOL.md`.

The MaixCAM2 transport borrows the communication hardening proven on
MaixCAM Pro, adapted to the hardware SPI2/CS1 controller:

- 100 kHz default, bounded retries, and frame magic/length/CRC validation;
- one-byte leading-glitch recovery for shifted STATUS responses;
- keep sending HELLO until STATUS reports `LINK_ACTIVE`, so an STM32 reset or
  a 500 ms link timeout recovers without restarting the app;
- `/tmp/stm32-spi2.lock` prevents a stale process from interleaving frames.

The Pro test's English-proxy KWS set is intentionally not used here:
MaixCAM2 keeps its SenseVoice model and the Chinese `keywords.json`
(长/春/昂/长春).

On a recognized `长春` phrase the app sends:

```text
PERFORM GATE, glyph_mask=0x3
```

and schedules a `RELEASE` after the configured demo hold time. The STM32 owns
the PWM state machine and 15-channel output.

2026-11-19 实测（MaixCAM2 SPI2/CS1 + `PWM-Heater-web` 固件）：

- STM32 STATUS 返回 `LINK_ACTIVE`；
- SenseVoice 识别 `长春` 后发送 `PERFORM GATE mask=0x3 event=1`；
- 1.2 s 后发送 `PERFORM RELEASE mask=0x3 event=2`；
- MaixCAM2 读回 STM32 STATUS `active_mask=0x3`，GATE/RELEASE 事件序号均被确认。

传输层会对偶发坏帧进行有界重试，控制事件不会因为单次坏帧丢失。

## USB microphone visualizer

`audio_ui/` contains a standalone 640 × 480 UI for the external USB
microphone. It shows the input level in dBFS, a peak-hold meter, a 140 ms
waveform, and a 24-band spectrum (60 Hz–8 kHz).

```sh
cd /root/JLU_LEDLAB/audio_ui
./run_mic_ui.sh
```

The launcher is stopped while the UI owns the display and restarted on exit.
If the USB port was in device mode, it is restored to device/gadget mode after
the UI exits. See `audio_ui/README.md` for options and troubleshooting.

The USB microphone voice app and `audio_ui` cannot run at the same time because
both need exclusive access to the USB capture device.

## “长春” voice business

```text
USB / built-in microphone
  -> SenseVoice Chinese recognition
  -> match 长春 / 长 + 春
  -> PERFORM GATE mask=0x3
  -> STM32 PWM1 + PWM2 (长/春)
  -> after hold_seconds: PERFORM RELEASE
```

All PWM is generated by the STM32. MaixCAM2 only sends SPI commands and
periodic temperature/heartbeat frames.

### Voice recognition scope

SenseVoice uses the external model in `/root/models/sensevoice-maixcam2/`;
the hot words are:

```text
长, 春, 昂, 长春
```

| Type | Content | Gates lamp |
|---|---|---|
| phrase | 长春、长 春 | yes, GATE `mask=0x3` |
| family chang | 长、常、场、厂、唱、昌、畅、尝、肠、偿、敞、倡 | single char logged only |
| family chun | 春、纯、唇、醇、淳、蠢、椿、鹑 | single char logged only |
| family ang | 昂、肮、盎 | debug only |
| combination | chang + chun in one utterance | yes, GATE `mask=0x3` |

Tone labels `chang1/chang2/chang4`, `chun1/chun2/chun3`, `ang1/ang2`
remain in the config as compatibility metadata; SenseVoice returns Chinese
text without tone labels, so matching uses the character aliases.
