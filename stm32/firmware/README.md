# STM32 固件

## Web 直控固件（新增）

在测试扩展固件上增加：

- `SET_LAMPS = 0x11`：13 个 u16 亮度值，直接设置 PWM1–PWM13；
- CAPS feature bit 5 `DIRECT = 1`；
- DIRECT 模式下 HEAT1/HEAT2 仍固定 0%；
- PERFORM / ALL_OFF 会退出 DIRECT 模式。

| 文件 | SHA256 |
|---|---|
| `PWM-Heater-web.hex` | `d96cba96150fd94f0a8097b27ec11771cd0270c70fd4cc50e48849589223c6a7` |
| `PWM-Heater-web.bin` | `b1d268529084ee431a35fbf1d862a7741d3fca3801f88621c1f97d6805e0be91` |

烧录：

```sh
st-flash --reset write stm32/firmware/PWM-Heater-web.bin 0x08000000
```

2026-11-08 目标板已烧录该校验版本（17,312 B）并通过 `st-flash` 校验。
本版在 Web 直控基础上增加了：

- SPI1 从机改为 **软件 NSS**（`SSM=1, SSI=0`）。总线上只有 STM32 一个
  从机时，外部 CS 线即使松动也不会挡住 64 字节事务；A24→PA4 的 CS 线
  仍可保留。
- 增加 SPI 超时/首次接收诊断打印，便于用 USART3 观察：
  `[spi] timeout state=... sr=... cr1=... cr2=... idr=... rx=...`；
- 收到合法控制帧后清除瞬时 `FAULT_SPI/CRC/FORMAT`，避免网页一直显示旧故障。

烧录后 CAPS features 应为 `0x2F`（含 `DIRECT`）。MaixCAM Pro 侧软件 SPI4
实测使用 **100 kHz** 最稳（200 kHz 也会偶发半帧）；网页直接亮度控制需要
同时运行 `tools/spi_service.py --frequency 100000`。


## 测试扩展固件（当前目录的 PWM-Heater.hex/bin）

在 v1 基线上增加：

- `GLYPH_ALL_LAMPS`（bit 2）：一次驱动 PWM1–PWM13，HEAT1/HEAT2 保持关闭；
- 收到 TEMP TLV 时通过 USART3 打印 `[temp] <mC> ...`。

| 文件 | SHA256 |
|---|---|
| `PWM-Heater.hex` | `72f1372f81f49463049e527f6e45dcd6625a7f9252671d62ac6ea15f4de5ccf8` |
| `PWM-Heater.bin` | `5fcb600380c41cd2619e35c9e5158ee10e6a86e7c56c53df17ff6707ff81ca78` |

2026-11-06 已用 ST-Link V2（`0483:3748`）通过 `st-flash 1.8.0` 烧录到目标板：
写入 16,784 B 到 `0x08000000`，日志为 `Flash written and verified!`，随后通过
AIRCR 软件复位（NRST 未连接）。MaixCAM Pro 尚未接入，因此 SPI、13 路灯牌和
USART3 温度打印仍需整机联调。

## 已验证 v1 基线

| 文件 | SHA256 |
|---|---|
| `PWM-Heater-v1-base.hex` | `4dce928e46e97023363e5d2e2e434276b8de960074f7d2a119dae60ab8bf07a5` |
| `PWM-Heater-v1-base.bin` | `4dd2531314719912d362bdd68c30d4070f1af02e951cdb218111c1f67ebab609` |

v1 基线已用 ST-Link V2 烧录并读回校验，烧录后 TIM2/TIM4 均已启动、CCR 全 0。

烧录：

```sh
st-flash --reset write PWM-Heater.bin 0x08000000
```

目标：

```text
STM32F103C8T6
Chip ID 0x0410
SRAM 20 KiB
Flash 128 KiB (st-link reported)
```
