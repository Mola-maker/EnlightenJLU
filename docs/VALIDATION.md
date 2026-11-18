# 验证记录与已知限制

本文件收纳各阶段的烧录/联调验证记录和已知限制，根 `README.md` 只保留摘要。
温控调试的详细过程见 [`THERMAL_PID_BRINGUP.md`](THERMAL_PID_BRINGUP.md)。

## 1. 烧录与固件校验

### 2026-11-04　STM32 基础固件

用 ST-Link V2 烧录并读回校验：

```text
PWM-Heater.bin
SHA256 4dd2531314719912d362bdd68c30d4070f1af02e951cdb218111c1f67ebab609
```

烧录后读到：

```text
TIM2 CR1=0x00000001 PSC=35 ARR=999  CCR1..4=0
TIM4 CR1=0x00000081 PSC=8  ARR=399  CCR1..3=0
```

说明固件已运行，15 路输出和 6 个功率电阻全部关闭。

### 2026-11-06　测试扩展固件

```text
PWM-Heater-test.bin
SHA256 5fcb600380c41cd2619e35c9e5158ee10e6a86e7c56c53df17ff6707ff81ca78
```

包含 `GLYPH_ALL_LAMPS`（PWM1–PWM13，排除 HEAT1/2）、USART3 `[temp]` 打印，
以及按最大占空比聚合灯牌输出的修正。

### 2026-11-08　Web 直控固件

```text
PWM-Heater-web.bin
SHA256 43836bb9bf82606f2734896d7902df2f8c76f5d53cd548f07f941c3e00c05450
```

烧录并校验通过，`direct_ready=true`，13 路 0–100% 亮度直控可用。

## 2. 整机联调（2026-11-08）

- 温控执行器加入 `THERMAL_CONFIG` / `HEATER`，HEAT1/HEAT2 可由 PID 命令控制；
- 软件 SPI4 采用 **100 kHz**；STM32 改为软件 NSS（单从机），解决外部 CS
  接触/片选导致的整帧超时；
- KWS 使用 USB 麦克风 card 2，10 cm / 30 cm「哈喽」可触发，触发后
  `active_mask=0x4`、PWM1–PWM13 执行 GATE/RELEASE；
- `/etc/init.d/S99surplife` 开机自动切换 USB host、启动 KWS + Web 服务；
- `tools/spi_service.py` 以 1 Hz 读取 SG2002 结温并发送 TEMP TLV，STATUS 回传
  `temp_mc`（约 49 °C，`temp_valid=true`），USART3 可观察；
- PID 调试改用命令行 `tools/temp_monitor.py` 实时打印结温（text/CSV/JSON +
  滚动 min/max/avg）；Web 控制台不再作为温度调试入口；
- 应用黑屏根因是 `rcS` 的 `S??*` 通配符把 `S06maixapp.*.bak` 当成启动脚本，
  多个 launcher 同时抢占 `/dev/cvi-vo`；备份已移到 `/root/surplife/init_backups`。

## 3. 温控与自动整定（2026-11-17 / 11-18）

- 手工整定基准点（环境 −4 °C、无加热结温 21 °C）：`hold 350‰`、`Kp 60`、
  `Ki ≈ 1.0`、预热带 4 °C，稳定在 43 °C；
- 该基准点被用作 `thermal_tuning.py` 的校准输入：`g = (43−21)/0.35 ≈ 63 °C`
  每满量程占空比；同一输入喂回模型得到 `hold 349‰ / Kp 63.5 / Ki 1.41 /
  预热带 3.3 °C`，与手工整定一致（`tests/test_thermal_tuning.py` 固化了这条对照）；
- 温控命令行 `tools/thermal_pid.py` 支持 `--ambient/--no-heat` 自动整定、
  dry-run、`--enable`、最大总功率、斜率预测、积分窗口和过温保护；
  手工 `--kp/--ki/--kd/--hold-duty/--warmup-band` 仅用于台架整定；
- 2026-11-18 多次复现“duty 回读仍为 600/500、STM32 未复位、温度反降”，
  定位为 5V 适配器/加热支路进入热保护；`--split-heat` 可降低峰值，但长时间
  高功率仍需稳定 5V/5A 级电源或间歇加热；
- 板上同时只能有一个进程持有 SPI4，详见 `docs/THERMAL_PID_BRINGUP.md` §2。

## 4. 已知限制

- 13 路灯牌的实际光输出尚未现场确认（SPI 控制链和 PWM 命令已验证）；
- ST-Link NRST 尚未接，建议后续调试补上；
- 保温板闭环仍受电源余量、SPI 接触和加热器/传感器热耦合限制：
  HEAT 执行、固件保护、暖启动 PI 与自动整定均已实现，但长时稳定性待现场确认；
- 过流硬件保护尚未实现；
- 一代 Pro 的 KWS 权重只存在设备上，未进入仓库；
- 语音业务与麦克风 UI 不能同时占用 USB 麦克风；
- 一代 Pro 约 128 MB 内存，后台语音和触摸 UI 不要同时长期运行；
- 1 m 远场「哈喽」概率约 0.03，低于 0.12 阈值；10/30 cm 稳定；
- 网页显示 `faults` 偶发瞬时 `0x4`，下一次合法帧清除；
- 校园网（`JLU.PC` 等）有客户端隔离，设备与 PC 无法互访，详见 `HANDOVER.md` §6；
- 不提交密钥、密码、个人语音录音或 token。
