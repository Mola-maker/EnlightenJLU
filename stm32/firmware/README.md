# STM32 固件

## Web 直控固件（新增）

在测试扩展固件上增加：

- `SET_LAMPS = 0x11`：13 个 u16 亮度值，直接设置 PWM1–PWM13；
- CAPS feature bit 5 `DIRECT = 1`；
- DIRECT 灯效与 HEAT 独立，HEAT 只由 `THERMAL_CONFIG`/`HEATER` 控制；
- PERFORM / ALL_OFF 会退出 DIRECT 模式。

| 文件 | SHA256 |
|---|---|
| `PWM-Heater-web.hex` | `6a96d1c909d9752d376744dd07a1b95e894aa24d9378a0ed65a5858269362713` |
| `PWM-Heater-web.bin` | `43836bb9bf82606f2734896d7902df2f8c76f5d53cd548f07f941c3e00c05450` |

烧录：

```sh
st-flash --reset write stm32/firmware/PWM-Heater-web.bin 0x08000000
```

如果 USB 枚举成 `0d28:0204 DAPLink CMSIS-DAP` 而不是
`0483:3748 ST-LINK/V2`，可用 OpenOCD：

```sh
openocd -f interface/cmsis-dap.cfg -f target/stm32f1x.cfg \
  -c "adapter speed 1000; init; reset halt; \
      flash write_image erase stm32/firmware/PWM-Heater-web.bin 0x08000000; \
      verify_image stm32/firmware/PWM-Heater-web.bin 0x08000000; \
      reset run; shutdown"
```

2026-11-19 已用 DAPLink CMSIS-DAP（`0d28:0204`，OpenOCD 0.12.0）写入同一
`PWM-Heater-web.bin`，OpenOCD 报告 device id `0x20036410`、flash 64 KiB；
回读 18,224 B 的 SHA256 与文件一致，随后 `reset run`。

2026-11-18 目标板已烧录并校验（18,224 B）。本版在 2026-11-17 温控执行器
基础上把 `HEATER` 的 duty 解释为两路合计功率，并在 `THERMAL_CONFIG`
保留字节 bit0 选择通道模式：

默认 `balanced`：HEAT1=HEAT2=duty；`split`（reserved bit0=1）时：

```text
d1 = min(1000, 2 * duty)
d2 = 2 * duty - d1
```

- balanced 600‰：两路各 600‰，合计约 9 W，5V 峰值约 3A，需要 5V/3A 以上；
- split 600‰：HEAT1=1000‰、HEAT2=200‰，合计约 9 W，峰值约 1.8A，适合 5V/2A；
- split 500‰：HEAT1=1000‰、HEAT2=0‰，合计约 7.5 W，峰值约 1.5A。

本版仍包含 Web 直控和温控执行器：

- SPI1 从机改为 **软件 NSS**（`SSM=1, SSI=0`）。总线上只有 STM32 一个
  从机时，外部 CS 线即使松动也不会挡住 64 字节事务；A24→PA4 的 CS 线
  仍可保留。
- 增加 SPI 超时/首次接收诊断打印，便于用 USART3 观察：
  `[spi] timeout state=... sr=... cr1=... cr2=... idr=... rx=...`；
- 收到合法控制帧后清除瞬时 `FAULT_SPI/CRC/FORMAT`，避免网页一直显示旧故障；
- 新增 `THERMAL_CONFIG=0x30`、`HEATER=0x31`：HEAT1/HEAT2 在安全包络内
  执行主机下发占空比；
- STATUS 新增 `heater_duty` 和 `heater_flags`（使能/活动/过温/温度无效/已配置）；
- 过温、温度过期、链路丢失、未配置或未使能时 HEAT 强制 0。

烧录后 CAPS features 应为 `0x3F`（含 `THERMAL_PID` 和 `DIRECT`）。MaixCAM Pro
侧软件 SPI4 生产配置使用 **100 kHz**；温控 PID 由
`maixcam/pro/test/tools/thermal_pid.py` 在命令行运行，STM32 只做执行器与
安全保护。


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
