# MaixCAM Pro SPI / 英文关键词灯效验证

本目录是 `/root/surplife/test` 的源代码。

## 1. 业务目标

```text
麦克风
  └─ Maix-Speech KWS（板上原有模型）
       ├─ hello   → SPI PERFORM GATE mask=0x4
       └─ welcome → SPI PERFORM GATE mask=0x4
                         │
                         ▼
              STM32F103：PWM1..PWM13 全亮
                         （HEAT1/HEAT2 永远不参与）
```

- 关键词触发后，STM32 的 `GLYPH_ALL_LAMPS` 逻辑字牌驱动 **PWM1–PWM13**；
- `HEAT1/HEAT2`（PB8/PB7，TIM4_CH3/CH2）不激活；
- 触发后保持 `hold_seconds=1.2`，随后发 `RELEASE`，回到 13 路灯牌的待机呼吸；
- MaixCAM 同时以 1 Hz 读取 SG2002 `/sys/class/thermal/thermal_zone0/temp`，
  通过 `TEMP` TLV 发给 STM32；
- STM32 的 USART3 本地调试口会打印 `[temp] ... mC ...`。

## 2. 重要限制：英文词与现有模型

板上已有模型 `am_3332_192_int8` 是 **中文拼音 KWS 模型**，不是英文 ASR：

- 直接 `speech.kws(["hello", "welcome"], ...)` 会报 `parse kws ... error`；
- 英文 TTS `hello/welcome` 在中文拼音候选下概率为 0；
- 本测试使用中文近音代理，日志仍显示英文业务标签：

| 业务标签 | 实际 KWS 拼音 | 说明 |
|---|---|---|
| `hello` | `ha1 lou2` | 哈喽 |
| `welcome` | `wei1 er2 kang1` | 威尔康 |

这可以在现有模型上验证关键词 → SPI → 全灯业务。若必须严格识别英文
发音，需要替换/新增英文 KWS 模型，不能只改 `kws` 字符串。

中文近音测试 WAV：

```text
wav/hallo_zh_16k_pad.wav
wav/weierkang_zh_16k_pad.wav
```

英文原始 TTS 也保留在 `wav/hello_16k_pad.wav`、`wav/welcome_16k_pad.wav`，
用于对比；当前模型不会触发它们。

## 3. 硬件连接

MaixCAM Pro 外部 SPI4：

| MaixCAM Pro | STM32 |
|---|---|
| A22 / SPI4_SCK | PA5 / SPI1_SCK |
| A23 / SPI4_MISO | PA6 / SPI1_MISO |
| A24 / SPI4_CS | PA4 / SPI1_NSS |
| A25 / SPI4_MOSI | PA7 / SPI1_MOSI |
| GND | GND |

Linux 设备：

```text
/dev/spidev4.0
```

MaixCAM Pro 的 A22-A25 是 **软件模拟 SPI4**（`spi4@gpio` + `spi_gpio`），
不是 `spidev3.0` 对应的硬件 SPI3。部分系统镜像启动后 `spi_gpio` 没有自动
绑定，需要先执行：

```sh
echo spi4@gpio > /sys/bus/platform/drivers/spi_gpio/bind
```

`spi4` 的 spidev 驱动会拒绝显式写 `spi.mode = 0`，并固定报告 CS_HIGH 标志；
因此程序只设置 `bits_per_word=8` 和 `max_speed_hz`，保持驱动默认 mode。

SPI 模式 0、8 bit，默认 500 kHz。打开设备后等待 0.5 s，让软件 SPI4
稳定；每个响应都会校验 magic、长度和 CRC，失败时最多重试 5 次，间隔
100 ms。

软件 SPI4 偶尔会在帧头前多送一个字节并丢掉末尾的保留 0，传输层会在
CRC 校验通过时自动去掉首字节并补零恢复。连续坏帧最终由 20 Hz 心跳和
HELLO 重建处理。ST-Link 不在本测试链路上；它只用于烧录 STM32。

## 4. 运行

实时麦克风 + SPI：

```sh
cd /root/surplife/test
./run_test.sh
```

### 4.1 正常业务：待机呼吸 + 关键词表演

软件 SPI4 是内核 bit-bang，KWS 模型加载和推理会明显干扰它的时序。
因此正常业务推荐把 **KWS 进程** 和 **SPI 进程** 分开：KWS 用
`--no-spi` 只输出关键词日志，`tools/spi_service.py` 独占 SPI4，默认让
PWM1–PWM13 处于 1%–8% 的 4 秒待机呼吸；检测到关键词后执行 1.2 秒
GATE/HOLD，再执行 RELEASE，最后回到待机呼吸。

```sh
cd /root/surplife/test
nohup sh -c 'python3 -u main.py --no-spi 2>&1 |
  tee /root/surplife/run/kws_live.log |
  python3 -u tools/spi_service.py --hold 1.2 --frequency 500000' \
  </dev/null >/root/surplife/run/spi_live.log 2>&1 &
```

停止时先停 SPI 服务，让它发送 `ALL_OFF`：

```sh
pkill -TERM -f 'tools/spi_service.py'
pkill -TERM -f 'main.py --no-spi'
```

正常待机状态应看到：

```text
link=2 active=0x0 glyphs=(0, 0, 1)
```

`active_mask=0` 是正常的，因为 STANDBY 不计入 active mask；第三个
glyph 状态 `1` 就是待机呼吸。

关键词触发时应看到：

```text
TRIGGER -> GATE mask=0x4 event=...
GATE ACK active=0x4 glyphs=(0, 0, 2)
PERFORM RELEASE mask=0x4
glyphs=(0, 0, 4) -> glyphs=(0, 0, 1)
```

`GATE` 的 100% 只是关键词后的短暂表演，不是默认占空比。
`tools/hold_all_lamps.py` 仅用于电气测试强制 100%，不要作为正常业务运行。

无 SPI，只验证 KWS：

```sh
./run_test.sh --no-spi --wav wav/hallo_zh_16k_pad.wav
./run_test.sh --no-spi --wav wav/weierkang_zh_16k_pad.wav
```

只验证 WAV，但仍尝试打开 SPI：

```sh
./run_test.sh --wav wav/hallo_zh_16k_pad.wav
```

停止：`Ctrl-C`。

## 5. STM32 端要求

本测试要求 STM32 固件包含：

- `GLYPH_ALL_LAMPS = 0x4`，驱动 PWM1..PWM13，不驱动 HEAT1/HEAT2；
- `TEMP` TLV 处理时通过 USART3 打印：

```text
[temp] 41350 mC seq=1 age=0ms valid=1 source=0
```

本目录已放入新测试固件和回退固件：

```text
firmware/PWM-Heater-test.hex        # 新：GLYPH_ALL_LAMPS + [temp] 打印
firmware/PWM-Heater-test.bin
firmware/PWM-Heater-v1-base.hex      # 旧：已验证基线
firmware/PWM-Heater-v1-base.bin
```

新固件 SHA256：

```text
PWM-Heater-test.bin
5fcb600380c41cd2619e35c9e5158ee10e6a86e7c56c53df17ff6707ff81ca78
```

烧录命令（在 ST-Link 所在主机上执行）：

```sh
st-flash --reset write firmware/PWM-Heater-test.bin 0x08000000
```

必须用 ST-Link 烧录 `PWM-Heater-test.bin` 到 STM32 后，SPI 才会返回有效
STATUS。2026-11-06 已在工作站上通过 ST-Link V2 烧录并校验通过；但 MaixCAM Pro
与 STM32 还未连接，SPI、13 路灯牌动作和 USART3 温度打印仍需整机验证。

## 6. 自检

```sh
python3 tools/probe_kws.py /root/models/am_3332_192_int8.mud \
  wav/hallo_zh_16k_pad.wav "ha1 lou2"
```

期望 `kws ret Err.ERR_NONE`，并看到约为 **0.63** 的最大概率。

SPI 探测：

```sh
python3 tools/probe_spi.py 4 0
```

探针会自动配置 A22-A25、绑定 `spi4@gpio`，并最多发送 10 次 HELLO。看到
`link=2`（ACTIVE）和有效的 STATUS/CAPS 即表示链路已通；若持续 `bad magic`
或 `0000`，优先检查 3.3V、共地、PA4–PA7 ↔ A22–A25 的接触，尤其是 A24/CS。

主机侧协议/坏帧恢复测试：

```sh
cd /root/surplife/test
python3 -m unittest -v tests/test_spi4_bootstrap.py
```


## 7. 本次实测状态

在 MaixCAM Pro 上：

- 模型可加载，中文近音 `ha1 lou2` 在线下 WAV 测试中最高概率约 **0.638**，
  `wei1 er2 kang1` 约 **0.158**；
- 原始英文 TTS `hello`/`welcome` 对中文拼音代理关键词概率为 **0**，
  说明现有模型不能直接做英文 KWS；
- SG2002 温度读取正常，实测约 **41.648 °C**，已构造 TEMP TLV；
- `/dev/spidev3.0` 能打开但 MISO 始终为 `00`，因为它不是外部 A22-A25
  对应的总线；
- 正确设备是 `/dev/spidev4.0`。绑定 `spi4@gpio` 后能与 STM32 交换 STATUS，
  STM32 已进入 `ACTIVE`，CAPS 为协议 v1、3 个 glyph、15 路 PWM、
  features `0x0F`；
- 软件 SPI4 在系统负载下会间歇性返回 `5A 5A`、`5A FF` 或全零响应；其中
  `5A 5A A5 ...` 是首字节偏移，可在 CRC 校验通过后恢复；
- 传输层已加入 0.5 s 稳定等待、CRC 校验、5 次重试和链路丢失后重新 HELLO，
  保留 20 Hz 心跳；
- 2026-11-08：已实测 STM32 返回有效 STATUS；当前无灯牌动作、无温度样本，
  `active_mask=0`，历史粘滞故障位为 `FORMAT | SPI`。
