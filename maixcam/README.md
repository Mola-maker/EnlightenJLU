# MaixCAM 业务代码

两代 MaixCAM 都负责音频、语音判断和高层表演决策；STM32 负责最终 15 路灯牌 PWM。

| 项目 | MaixCAM Pro（一代） | MaixCAM2（二代） |
|---|---|---|
| SoC | SG2002，约 128 MB | AX620E/AX630C，约 1 GB |
| 语音 | Maix-Speech KWS | SenseVoice ASR |
| 模型 | `/root/models/am_3332_192_int8.mud` | `/root/models/sensevoice-maixcam2/` |
| 板载 PWM | 无；全部由 STM32 产生 | 无；全部由 STM32 产生 |
| STM32 链路 | 软件 SPI4，A22–A25 | 硬件 SPI2/CS1，B18–B21 |
| 运行目录 | `/root/surplife/test` | `/root/JLU_LEDLAB` |
| 入口 | `test/main.py` | `app/main.py` |
| Web 中控台 | `test/tools/spi_service.py`，80/8080 | `tools/spi_service.py`，8080 |

```text
麦克风 → MaixCAM KWS/ASR → 高层 PERFORM/TEMP/HEARTBEAT → STM32 → 15 PWM
```

## 目录

```text
maixcam/
├── pro/
│   ├── VERSION
│   ├── README.md
│   ├── app/                 # sg2002_temp.py、ALSA 配置、温度测试
│   └── test/                # SPI4 KWS/Web/thermal 主线
└── maixcam2/
    ├── app/                 # SenseVoice + SPI2，无板载 PWM
    ├── config/              # keywords + SenseVoice descriptor
    ├── audio_ui/            # USB mic visualizer
    ├── usb_audio/           # snd-usb-audio scripts
    ├── bench/               # recognition/USB tests
    └── run/start/stop scripts
```

## 规范

- MaixCAM 不生成任何板载 PWM 波形，也不配置 A18/A19/A30 等 PWM 复用；
- MaixCAM 只发送 `glyph_mask`、`PERFORM` 动作、温度/心跳；
- STM32 负责 15 路 PWM、待机呼吸、GATE/HOLD/RELEASE/EGG 和 HEAT 安全包络；
- 模型权重不进入业务目录；
- 同一块板的语音业务和麦克风 UI 不能同时占用麦克风；
- 绝对路径是板端运行路径，不是仓库路径。
