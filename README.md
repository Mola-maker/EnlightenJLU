# EnlightenJLU

语音控制的灯牌 PWM + 保温板加热。**MaixCAM 负责音频、语音判断、温控算法与高层决策**，
**STM32F103C8T6 负责 15 路 PWM（13 路灯牌 + HEAT1/HEAT2）、安全看门狗与硬保护**。

## 1. 接手先看这里

| 我想…… | 从这里开始 |
|---|---|
| **把设备跑起来**（换 WiFi、找 IP、开网页、SSH） | [`docs/HANDOVER.md`](docs/HANDOVER.md) |
| 改板上的 Web / 温控代码 | [`maixcam/pro/test/README.md`](maixcam/pro/test/README.md) |
| 改 STM32 固件或 SPI 协议 | [`stm32/README.md`](stm32/README.md)、[`stm32/docs/SPI_PROTOCOL.md`](stm32/docs/SPI_PROTOCOL.md) |
| 看验证记录与已知限制 | [`docs/VALIDATION.md`](docs/VALIDATION.md)、[`docs/THERMAL_PID_BRINGUP.md`](docs/THERMAL_PID_BRINGUP.md) |
| 追溯代码资产来源 | [`docs/ASSET_INVENTORY.md`](docs/ASSET_INVENTORY.md) |

模块级文档：[`maixcam/README.md`](maixcam/README.md) · [`maixcam/pro/README.md`](maixcam/pro/README.md) · [`maixcam/maixcam2/README.md`](maixcam/maixcam2/README.md) · [`model/README.md`](model/README.md) · [`stm32/README.md`](stm32/README.md)

最短上手路径（设备已有固件，只想更新 Python 侧）：

```sh
git clone https://github.com/Mola-maker/EnlightenJLU.git
cd EnlightenJLU/maixcam/pro/test
scp -r . root@<设备IP>:/root/surplife/test/
ssh root@<设备IP> '/etc/init.d/S99surplife restart'    # 起 Web(80) + KWS
```

浏览器打开 `http://<设备IP>/`（80 被占用时自动退 8080）；设备 IP 可用
`http://maixcam-4e47.local/`（mDNS）或看设备屏幕。**换手机热点不用改代码**：
改 `/boot/wifi.ssid` + `/boot/wifi.pass`（FAT 分区，PC 上直接编辑，密码留空=开放网络）；
也可以在 Web 控制台左侧「网络」页直接填 SSID/密码（设备自己重连），
或多网络时写 `/boot/wifi.networks`（每行 `SSID|密码`，顺序即优先级）。

## 2. 系统架构

```text
麦克风
  ├─ MaixCAM Pro（一代）── Maix-Speech KWS ──┐
  └─ MaixCAM2（二代）──── SenseVoice ASR ───┤
                                             ▼
                        PERFORM / TEMP / HEARTBEAT（SPI v1，64 字节帧）
                                             ▼
                             STM32F103C8T6 执行器
                        15 路灯牌 PWM + 2 路加热 PWM
                                             ▼
                                灯牌 / 保温板（6 个功率电阻）
```

设计原则：

- 语音识别与判断在 MaixCAM，STM32 只接受数字化的 `glyph_mask`、动作与温度；
- STM32 上电未握手、SPI 断链、`ALL_OFF`、温度无效/超温时，所有输出关闭；
- 两代 MaixCAM 都保留本地 PWM 作为脱机联调回退，不替代 STM32 的 15 路输出。

## 3. 目录结构

```text
EnlightenJLU/
├── docs/            # HANDOVER（上手）/ VALIDATION（验证与限制）/ THERMAL_PID_BRINGUP / ASSET_INVENTORY
├── model/           # KWS / SenseVoice 描述、运行支撑、清单（大权重外置）
├── maixcam/
│   ├── pro/         # 一代 MaixCAM Pro：KWS、门控、本地 PWM、触摸 UI
│   │   └── test/    # ← 当前主线：Web 控制台 + 温控 + SPI 服务（板上 /root/surplife/test）
│   └── maixcam2/    # 二代 MaixCAM2：SenseVoice、SPI、USB 麦克风
└── stm32/           # HAL/CMSIS、15 路 PWM、SPI v1、表演引擎、已验证固件
```

资产数量/大小等明细见 [`docs/ASSET_INVENTORY.md`](docs/ASSET_INVENTORY.md)。

## 4. 快速开始

### 4.1 一代 MaixCAM Pro（KWS + 灯牌演出）

```sh
cp -a maixcam/pro/app/. /root/surplife/app/       # 权重留在 /root/models/am_3332_192_int8.mud
cd /root/surplife/app && ./run_voice_pwm.sh       # 前台
./start_voice_pwm.sh                              # 或后台：start / status / stop
```

> 一代板上的触摸 UI（`lamp_ui.py`）已于 2026-11-18 删除：它在 128 MB 的 Pro 上
> 与语音模型抢内存（需要靠 `oom_score_adj` 抢优先级），实际灯牌调试已由
> `maixcam/pro/test/` 的 Web 控制台承担。需要旧 UI 可从 Git 历史取回。

### 4.2 二代 MaixCAM2（SenseVoice + 本地临时灯效）

```sh
cp -a maixcam/maixcam2/. /root/JLU_LEDLAB/
cp -a model/maixcam2/sensevoice-maixcam2/. /root/models/sensevoice-maixcam2/
cd /root/JLU_LEDLAB && ./start_usb_local.sh       # ./stop.sh 停止
./audio_ui/run_mic_ui.sh                          # 麦克风可视化（与语音业务互斥）
```

### 4.3 STM32 固件

```sh
cd stm32 && cmake --preset Release && cmake --build --preset Release
st-flash --reset write firmware/PWM-Heater-web.bin 0x08000000    # Web 直控固件
```

构建/硬件/协议细节：[`stm32/docs/BUILD.md`](stm32/docs/BUILD.md)、
[`stm32/docs/HARDWARE.md`](stm32/docs/HARDWARE.md)、
[`stm32/docs/SPI_PROTOCOL.md`](stm32/docs/SPI_PROTOCOL.md)。

## 5. 温控：操作者只填两个温度

`Kp/Ki/Kd` 属于实现细节，**不再作为用户输入**。使用者只提供：

| 输入 | 含义 |
|---|---|
| `环境温度` | 测试环境（冷室 / 房间）温度 |
| `无加热结温` | 加热全关时板子稳定后的结温（含 SoC 自热） |

`thermal_tuning.py` 用一阶+纯滞后模型推导前馈占空比、预热带与 PI 增益，再下发给
STM32 执行。校准来自 2026-11-17 实测：同一工况自动推导出 `hold 349‰ / Kp 63.5 /
Ki 1.41 / 预热带 3.3 °C`，与当时手工整定的 `350‰ / 60 / 1.0 / 4 °C` 一致。
目标不可达时会明确告警并给出满功率预计最高温度。

```sh
curl -X POST http://<设备IP>/api/thermal -H 'Content-Type: application/json' \
  -d '{"ambient_c":-20,"no_heat_junction_c":5,"max_duty":1000,"enabled":false}'
curl -s http://<设备IP>/api/thermal        # 看推导结果与警告
```

详见 [`maixcam/pro/test/README.md`](maixcam/pro/test/README.md) §4.2 与
[`docs/HANDOVER.md`](docs/HANDOVER.md) §5（−20 °C 冷室注意事项）。

## 6. 必须知道的约束

- **SPI4 独占**：`spi_service.py`（Web + 灯牌 + 温控）与 `thermal_pid.py` /
  `temp_monitor.py` 不能同时运行，抢锁会报 `SPI bus 4 is already owned`。
  跑命令行 PID 前先 `/etc/init.d/S99surplife stop`，跑完再 `start`；
- **USB-C 角色**：`S99surplife start` 会把 USB-C 切成 host（为 USB 麦克风），
  若还要用 USB-NCM 网口，改用 `/root/surplife/start_web_only.sh`；
- **USB 麦克风独占**：语音业务与麦克风 UI 不能同时占用；
- **供电**：默认 `max_duty 500‰`（5V/2A 保守值）；600‰ balanced 需 5V/3A 以上，
  5V/2A 场景用 `--split-heat`；长时间高功率建议 5V/5A 或间歇加热；
- **不要提交**密钥、密码、个人录音或 token。

## 7. 已知限制（摘要）

灯牌实际光输出待现场确认；ST-Link NRST 未接；保温板长时稳定性受电源/热耦合限制；
无过流硬件保护；一代 Pro 权重只在设备上；校园网有客户端隔离，设备与 PC 无法互访。

完整清单与烧录校验记录见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。

## 8. 许可与第三方代码

STM32 HAL/CMSIS、MaixPy/MaixCDK、SenseVoice 支撑代码等第三方组件遵循各自许可证，
使用前请核对原始项目许可；本仓库不额外声明第三方代码的所有权。
