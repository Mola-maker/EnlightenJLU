# MaixCAM Pro（一代）SPI 业务

本目录只保留 MaixCAM Pro 的 SPI 高层控制代码和共享辅助代码。**MaixCAM Pro
不驱动任何板载 PWM 引脚**：所有 PWM1–PWM13、HEAT1/HEAT2、待机呼吸和
GATE/HOLD/RELEASE/EGG 状态机都由 STM32 产生，Pro 只通过 SPI4 发送
`HELLO`、`PERFORM`、`TEMP`、`HEARTBEAT` 等高层命令。

2026-11-19 已删除旧的本地 PWM 业务代码（`voice_pwm.py`、A18/PWM6、
A19/PWM7 sysfs 控制、`live_kws.py` 及其启动脚本），不再保留板载 PWM 输出路径。

## 目录

```text
maixcam/pro/
├── VERSION
├── README.md
├── app/
│   ├── sg2002_temp.py          # SG2002 温度读取
│   ├── asoundrc.surplife       # ALSA 麦克风分流配置
│   ├── README_TEMP_SENSOR.md   # 温度寄存器/换算说明
│   └── tests/test_sg2002_temp.py
└── test/
    ├── main.py                 # KWS + SPI PERFORM/TEMP/HEARTBEAT
    ├── spi_link.py             # /dev/spidev4.0 transport
    ├── spi4_bootstrap.py       # spi4@gpio 绑定
    ├── stm32_protocol.py
    ├── run_test.sh
    ├── tools/                  # probe/spi_service/thermal
    └── web/                    # 本地 Web 控制台
```

`test/main.py` 通过 `sys.path` 使用 `app/sg2002_temp.py`；该文件只是温度
读取辅助，不包含 PWM 代码。

## 部署

```text
仓库 maixcam/pro/app/   →  设备 /root/surplife/app/
仓库 maixcam/pro/test/  →  设备 /root/surplife/test/
```

```sh
mkdir -p /root/surplife/run /root/surplife/logs /root/surplife/artifacts
```

模型保持在设备上：

```text
/root/models/am_3332_192_int8.mud
```

## 运行

前台 SPI 测试/业务：

```sh
cd /root/surplife/test
./run_test.sh
```

只验证 KWS 和 SPI，不接实际灯牌：

```sh
./run_test.sh --no-spi --wav wav/hallo_zh_16k_pad.wav
```

Web 控制台 + SPI 服务：

```sh
cd /root/surplife
python3 test/tools/spi_service.py
```

开机自启由 `test/tools/S99surplife` 管理。

## 硬件

MaixCAM Pro 使用软件 SPI4：

| MaixCAM Pro | STM32 |
|---|---|
| A22 / SPI4_SCK | PA5 / SPI1_SCK |
| A23 / SPI4_MISO | PA6 / SPI1_MISO |
| A24 / SPI4_CS | PA4 / SPI1_NSS（最新固件软件 NSS 可保留） |
| A25 / SPI4_MOSI | PA7 / SPI1_MOSI |
| GND | GND |

Linux 设备：`/dev/spidev4.0`。A22–A25 是 `spi4@gpio` 软件 SPI，不是硬件
SPI3 的 `/dev/spidev3.0`；`test/spi4_bootstrap.py` 会在需要时绑定
`spi_gpio` 驱动。

## KWS 代理词

板上模型是 Maix-Speech 中文拼音模型，不是英文 ASR。`test/main.py` 用
中文近音代理验证英文业务标签：

| 业务标签 | KWS 拼音 |
|---|---|
| hello | `ha1 lou2` |
| welcome | `wei1 er2 kang1` |

这组代理词只属于 Pro 的 `test/`，**不会复制到 MaixCAM2**；MaixCAM2 使用
SenseVoice 和中文 `长/春/长春` 词表。

## 测试

```sh
cd maixcam/pro/app
python3 -m unittest -v tests/test_sg2002_temp.py

cd ../test
python3 -m unittest -v tests/test_spi_link_recovery.py tests/test_spi4_bootstrap.py
```

实际灯牌/温控联调必须在 MaixCAM Pro + STM32 SPI4 硬件上执行。
