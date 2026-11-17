# 温控 PID 调试进度（2026-11-17）

## 当前硬件/软件状态

- MaixCAM Pro：`192.168.43.229`，`root/root`，SPI4 `/dev/spidev4.0`，100 kHz；
- STM32F103C8T6：`PWM-Heater-web.bin`，含 `THERMAL_CONFIG=0x30`、
  `HEATER=0x31`、STATUS `heater_duty/heater_flags/uptime`；
- 加热负载：HEAT1/HEAT2 各 3×10 Ω 并联，100% 合计约 3 A / 15 W；
- 主机控制器：`maixcam/pro/test/tools/thermal_pid.py`；
- 当前测试参数：`--setpoint 43 --margin 3 --enable --max-duty 600 --hold-duty 350`。

## 已修复/验证

### 1. 链路超时后的配置丢失

旧逻辑在 `HELLO` 和 `THERMAL_CONFIG` 同时待发送时只发 HELLO，STM32 会停在
`ACTIVE` 但 `configured=0`、`HEAT=0`；如果 SPI 又连续坏帧，主机只显示上一帧
的 `link=2/heat=599`，看起来像“温控自己停了”。

现在：

- `HELLO + THERMAL_CONFIG + HEATER` 可放在同一帧（payload 36 B，上限 44 B）；
- STATUS 显示未配置/未使能时，链路层自动重发安全包络；
- 连续坏帧会重新打开 `/dev/spidev4.0`；
- 日志增加 `up=`（STM32 uptime）、`rst=`、`fault=0x...` 和真实的 `link=DOWN`。

验证：dry-run 运行时用 ST-Link 复位 STM32：

```text
STM32 reset detected: uptime 809927ms -> 431ms (count=1)
14:22:11 ... flags=0x10 [configured] ... link=2 up=0s rst=1 DRY-RUN
```

复位后自动恢复配置，没有持续 `bad magic`。

### 2. 两个进程同时占用 SPI

S99 旧版 stop 只杀 Python，外层 `while true` 会残留；反复 restart 后设备上曾
积累 6 个 supervisor。与此同时手工启动 `thermal_pid.py` 就可能有两个 SPI
master，互相覆盖 session_id，STM32 反复 reset 配置并返回 `flags=0x00`。

现在：

- `S99surplife` 用 `setsid` 启动 supervisor，stop 杀整个进程组；
- `Stm32SpiLink` 打开 `/tmp/stm32-spi4.lock` 并持有 `flock`；
- 第二个进程会直接失败：

```text
thermal_pid: SPI bus 4 is already owned by another process (/tmp/stm32-spi4.lock)
```

### 3. 积分饱和

新增 `thermal_pi.py`：离目标超过 `--warmup-band` 时开环 `max_duty`，积分器固定
在 `--hold-duty`；进入目标前 2 °C 后才从 350‰ 开始 PI，避免冷启动把 600‰
累积进积分。

## 2026-11-17 14:24–14:31 实测

条件：环境/锚点记录为 -4 °C / 21 °C，`max=600`、`hold=350`、`Ki=1.0`。

```text
14:24:33 T=31.859C err=+11.14C req=600 heat=600 WARMUP
14:25:46 T=32.908C err=+10.09C req=600 heat=600 WARMUP
14:27:49 T=38.152C err=+4.85C  req=600 heat=600 WARMUP
14:29:28 T=41.298C err=+1.70C  req=456 heat=456 RUN
14:30:30 T=42.697C err=+0.30C  req=439 heat=439 RUN
14:31:16 T=39.900C err=+3.10C  req=600 heat=600 WARMUP
```

关键观察：

- 本次全程 `rst=0`，没有 STM32 复位，也没有持续 `bad magic`；
- 约 6 分钟从 31.9 °C 升到最高 42.697 °C，没有到 43 °C；
- 在 `heat=600` 实际回读的情况下，最后约 30 s 温度从 42.7 °C 掉到 39.9 °C；
- 温度平台/下降期间积分从 350‰ 涨到约 459‰，随后因低于 40.5 °C 重新进入
  WARMUP。说明当前瓶颈不只是 PI 参数：温度探头与加热器的热耦合、5 V 电源
  瞬态/限流、或 SG2002 结温受主控负载影响，都需要现场确认。

## 下次继续

1. 用万用表/示波器测 600‰ 时 HEAT1/HEAT2 对地电压和 5 V 母线；确认电源没有
   限流/热保护，确认加热器回流不经过 MCU/SPI 地。
2. 确认温度探头贴附位置；如果测的是 SG2002 结温，记录 MaixCAM CPU 负载，
   判断 42.7→39.9 °C 的下降是否来自主控负载变化而不是加热器。
3. 若要继续调算法，优先给积分加“接近目标才积分”的窗口/死区，或降低 Ki；
   当前 `Ki=1.0` 在温度平台期仍会积累约 1.7‰/s。
4. 重新跑长时测试时先 dry-run 验证链路，再逐步提高 `--max-duty`，并观察
   `up/rst/fault/heat` 四个字段。
5. 当前设备已恢复 `S99surplife` 服务，HEAT=0、enabled=False，可安全离开。
