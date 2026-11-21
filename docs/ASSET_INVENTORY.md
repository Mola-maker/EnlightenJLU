# 资产清单与迁移记录

生成时间：2026-11-06  
项目：语音控制的灯牌 PWM 效果  
仓库：`Mola-maker/EnlightenJLU`，`main` 分支

## 1. 来源与处置

| 来源 | 内容 | 处置 |
|---|---|---|
| `/home/jlu/桌面/EnlightenJLU/surplife/app` | 一代 Pro KWS、门控、A18/A19 PWM、UI、温度读取 | SPI/KWS/温度辅助迁入 `maixcam/pro/`；本地 PWM 已删除 |
| `/home/jlu/桌面/CubeMX/C8T6/PWM-Heater/maixcam2_app` | 二代 SenseVoice、SPI、USB 麦克风、A30/PWM6、UI | 迁入 `maixcam/maixcam2/`；本地 PWM 已删除 |
| `/home/jlu/桌面/CubeMX/C8T6/PWM-Heater` | STM32 15 路 PWM、SPI、表演引擎、保温板占位 | 迁入 `stm32/`，排除 build 和 MaixCAM 子目录 |
| MaixCAM2 `/root/models/sensevoice-maixcam2` | SenseVoice 权重和运行支撑 | 支撑文件复制到 `model/maixcam2/`；权重外置 |
| MaixCAM Pro `/root/models/am_3332_192_int8.mud` | 一代 KWS 权重/描述 | 未复制；仅记录 |

清理前目标目录所在文件系统在当前会话中是只读挂载，未能生成额外本地备份；旧版本仍可通过远端 Git 历史和一代 MaixCAM Pro 设备追溯。

### 2026-11-19 去除两代 MaixCAM 的板载 PWM 输出

MaixCAM 不再承担任何板载 PWM 输出，只通过 SPI 发送高层命令；15 路灯牌、
HEAT1/HEAT2、呼吸和 GATE/HOLD/RELEASE/EGG 全部由 STM32 执行。

已从仓库删除：

- Pro：`app/voice_pwm.py`、`app/live_kws.py`、`app/README_pwm.md`、
  `app/run_voice_pwm.sh`、`app/start_voice_pwm.sh`、`app/status_voice_pwm.sh`、
  `app/stop_voice_pwm.sh`、`app/tests/test_voice_pwm.py`；
- MaixCAM2：`app/local_pwm.py`、`start_usb_local.sh`，以及 `main.py` 中的
  `--local-pwm` / `--pwm-pin` / `--pwm-id` / `--pwm-freq` 参数。

Pro 的 SPI 主线保留在 `maixcam/pro/test/`，MaixCAM2 的 SPI 主线保留在
`maixcam/maixcam2/app/`。

## 2. 新结构

```text
EnlightenJLU/
├── README.md
├── .gitignore
├── docs/ASSET_INVENTORY.md
├── model/
├── maixcam/
│   ├── pro/
│   └── maixcam2/
└── stm32/
```

## 3. MaixCAM Pro（一代）

来源：`EnlightenJLU/surplife/app`  
目标：`maixcam/pro/app/`

当前 `app/` 只保留非 PWM 辅助：

| 文件 | 作用 |
|---|---|
| `sg2002_temp.py` | SG2002 温度读取 |
| `asoundrc.surplife` | ALSA file 插件麦克风分流 |
| `README_TEMP_SENSOR.md` | 温度寄存器和换算 |
| `tests/test_sg2002_temp.py` | 温度换算测试 |
| `VERSION` | 一代业务版本 `0.4.0`（SPI-only） |

已删除：

| 文件 | 原因 |
|---|---|
| ~~`voice_pwm.py`~~ | ~~KWS + A18/PWM6 + A19/PWM7 本地 PWM~~ → **2026-11-19 删除** |
| ~~`live_kws.py`~~ | ~~依赖 voice_pwm 的 KWS 观察器~~ → **2026-11-19 删除** |
| ~~`README_pwm.md`~~ | ~~本地 PWM 接线/曲线~~ → **2026-11-19 删除** |
| ~~`run_voice_pwm.sh` / `start_voice_pwm.sh` / `stop_voice_pwm.sh` / `status_voice_pwm.sh`~~ | ~~本地 PWM 运行管理~~ → **2026-11-19 删除** |
| ~~`tests/test_voice_pwm.py`~~ | ~~本地 PWM 逻辑测试~~ → **2026-11-19 删除** |
| ~~`lamp_ui.py` / `run_lamp_ui.sh` / `README_lamp_ui.md`~~ | ~~触摸 UI~~ → **2026-11-18 删除** |

规模：

```text
sg2002_temp.py    166 行
当前 PWM 输出代码   0 行（全部由 STM32 产生）
```

### 2026-11-18 移除一代板上触摸 UI

一代板上的 `lamp_ui.py`（2375 行，640×480 触摸 UI）、`run_lamp_ui.sh` 与
`README_lamp_ui.md` 已从仓库和板上删除：

- 它在 128 MB 内存的 MaixCAM Pro 上必须靠 `oom_score_adj=500` 抢优先级，
  与语音模型争内存，容易触发 OOM；
- 灯牌与温控的实际调试入口已迁移到 `maixcam/pro/test/` 的 Web 控制台
  （浏览器 + SSE 实时波形），板上 UI 属重复实现，留着只会让人误以为还有两条并行路径；
- 需要旧 UI 时从 Git 历史取回（`git log -- maixcam/pro/app/lamp_ui.py`）。
- `live_kws.py` 和依赖它的 `voice_pwm.py` 已于 2026-11-19 一并删除，不再保留
  任何本地 PWM 参考实现。

### 2026-11-18 板上目录对齐仓库（历史记录）

板端 `/root/surplife/app/` 当时与本仓库 `maixcam/pro/app/` 逐文件一致。
清掉的更早 Surplife 遗留件（从未进入本仓库）归档在板上
`/root/surplife/archive/legacy-surplife/`：

- `ble_client.py`、`protocol.py`、`main.py` —— 旧 BLE/HTTP 窗帘主程序；
- `voice_kws.py`、`voice_pwm_v027.py`、`README_pwm_v027.md` —— 更早的 v0.27 语音实现；
- `run_maixcam.sh`、`run_voice.sh` —— 旧入口脚本；
- `static/index.html` —— 旧 Surplife Web 页面；
- `tests/test_protocol.py`、`tests/test_voice.py`、`ui_probe.py` —— 对应上述旧代码与已删除的板上 UI。

顺带用仓库版本覆盖了板上两处陈旧副本（`live_kws.py`、`README_TEMP_SENSOR.md`）。
清理前已确认 `/etc/init.d/S99surplife` 用的是 `/root/surplife/test/main.py`（当前主线），
与被删的 `app/main.py` 无关，自启链不受影响。

当前仓库只剩一条 Pro 运行主线：

- `/root/surplife/app` —— 仅保留 `sg2002_temp.py`、`asoundrc.surplife` 等辅助；
- `/root/surplife/test` —— Web 控制台 + 温控 + SPI 服务（`S99surplife` 开机自启）。

2026-11-19 仓库已删除 Pro 的本地 PWM 代码；Pro 板端若当时离线，需在下次
上线后删除同一批文件，`app/` 目录只保留温度/ALSA 辅助。

板端运行路径保持：

```text
/root/surplife/app
/root/surplife/run
/root/surplife/logs
/root/surplife/artifacts
```

模型：

```text
/root/models/am_3332_192_int8.mud
```

KWS 词表：

```text
chang1 chang2 chang4
chun1 chun2 chun3
ang1 ang2
chang2 chun1
```

## 4. MaixCAM2（二代）

来源：`PWM-Heater/maixcam2_app`  
目标：`maixcam/maixcam2/`

| 文件/目录 | 作用 |
|---|---|
| `app/main.py` | SenseVoice、关键词匹配、VAD、SPI 高层命令发布 |
| `app/keyword_matcher.py` | “长/春/长春”字符族和整词匹配 |
| ~~`app/local_pwm.py`~~ | ~~A30/PWM6 本地表演状态机~~ → **2026-11-19 删除** |
| `app/stm32_protocol.py` | SPI v1 帧/TLV/CRC 参考实现 |
| `app/maixcam_spi_link.py` | SPI2/CS1 主机传输 |
| `app/test_keyword_matcher.py` | 关键词匹配单元测试 |
| `config/keywords.json` | 注册词、`glyph_mask=3`、保持 1.2 s |
| `config/sensevoice.mud` | 外部 SenseVoice 描述符 |
| `audio_ui/app/mic_ui.py` | 640×480 麦克风电平/波形/频谱 UI |
| `audio_ui/run_mic_ui.sh` | host/launcher 切换和 UI 启动 |
| `audio_ui/preview.png` | UI 预览 |
| `usb_audio/enable_usb_mic.sh` | USB host + `snd-usb-audio` |
| `usb_audio/disable_usb_mic.sh` | 恢复 device/gadget |
| `usb_audio/record_usb_mic.sh` | USB 麦克风录音和 RMS/峰值 |
| `bench/REPORT.md` | 13 字标语 SenseVoice 评测 |
| `bench/USB_MIC_TEST.md` | USB Audio 测试记录 |
| `run_voice.sh` / `start.sh` / `stop.sh` | 运行和后台管理 |
| `start_usb_stm32.sh` | USB 麦克风 + SenseVoice + STM32 SPI |
| `start_web.sh` / `stop_web.sh` | Web 中控台 + SPI 语音管线 |
| `tools/spi_service.py` | SPI2 owner + Flask 中控台（8080） |
| `tools/web_control.py` | 13 路灯牌/直控波形控制 |
| `tools/thermal_control.py` / `thermal_pi.py` / `thermal_tuning.py` | 温控状态和 PI |
| `tools/console_web.py` / `wifi_info.py` | Web API/页面与 WiFi 只读状态 |
| `tools/S99jluweb` | `/etc/init.d/S99jluweb` 开机启动 |
| `web/index.html` / `app.js` / `style.css` | 中控台静态页面 |
| ~~`start_usb_local.sh`~~ | ~~USB 麦克风 + A30/PWM6~~ → **2026-11-19 删除** |
| `README.md` | 二代运行说明 |

核心代码规模：

```text
app/main.py                    约 420 行（SPI-only）
app/stm32_protocol.py          232 行
app/maixcam_spi_link.py        约 290 行
audio_ui/app/mic_ui.py         850 行
本地 PWM 输出代码               0 行
```

板端运行路径保持：

```text
/root/JLU_LEDLAB
```

模型：

```text
/root/models/sensevoice-maixcam2/
```

## 5. 模型

### 已纳入仓库的 MaixCAM2 支撑

目录：`model/maixcam2/sensevoice-maixcam2/`

| 文件 | 大小 |
|---|---:|
| `SenseVoiceAx.py` | 23,098 B |
| `am.mvn` | 11,203 B |
| `chn_jpn_yue_eng_ko_spectok.bpe.model` | 377,341 B |
| `client.py` | 11,254 B |
| `embeddings.npy` | 25,523 B |
| `frontend.py` | 17,726 B |
| `model.mud` | 317 B |
| `pe_nonstream.npy` | 573,568 B |
| `pe_streaming.npy` | 58,368 B |
| `server.py` | 11,282 B |
| `tokenizer.py` | 4,810 B |

完整 SHA256 见 `model/manifest.json`。

### 外置权重

| 平台 | 文件 | 大小 | SHA256 |
|---|---|---:|---|
| MaixCAM2 | `sensevoice.axmodel` | 256,550,253 B | `67d290cf7cebf45db5f37b2e93b8bdfff44dc35110bb29d84204a5f9eae9fd4d` |
| MaixCAM2 | `streaming_sensevoice.axmodel` | 249,023,211 B | `ba1ddd60841297903bfdae059ad88092d0fd1c543e1d80d7f64199d4e27b8263` |
| MaixCAM Pro | `am_3332_192_int8.mud` + 配套 | 未获取 | 未获取 |

`model/**/*.axmodel` 等权重已在 `.gitignore` 中排除。

## 6. STM32

来源：`/home/jlu/桌面/CubeMX/C8T6/PWM-Heater`  
目标：`stm32/`

纳入：

- `Core/Inc`、`Core/Src`；
- `Drivers/`；
- `cmake/`、`CMakeLists.txt`、`CMakePresets.json`；
- `PWM-Heater.ioc`、`.mxproject`；
- `STM32F103xx_FLASH.ld`、`startup_stm32f103xb.s`；
- `docs/SPI_PROTOCOL.md`、`docs/BUILD.md`、`docs/HARDWARE.md`；
- `tools/`；
- `firmware/PWM-Heater.hex`、`.bin`。

排除：

- `build/`；
- `maixcam2_app/`；
- 临时原理图裁剪图片。

文件规模：

```text
946 个文件，约 69 MB（主要 Drivers/CMSIS/HAL）
```

已验证固件：

```text
PWM-Heater.bin   16,576 B
SHA256 4dd2531314719912d362bdd68c30d4070f1af02e951cdb218111c1f67ebab609
```

行为：

- 15 路 PWM 上电全部 0；
- HEAT1/HEAT2 固定 0%；
- 无 SPI HELLO 时全关；
- 500 ms 超时全关；
- 温度只接收并回显，不做 PID。

## 7. 明确移除的旧内容

- `surplife/archive/` 全部历史副本；
- `surplife/pwm.log`、`server.log`、`voice.log`、`logs/`；
- `surplife/artifacts/` 截图和 KWS 输出；
- `surplife/app/main.py`、`ble_client.py`、`protocol.py`、`static/` 等 BLE/HTTP 窗帘控制；
- `config/` 主机端语音关键词记录器；
- `light-dots/`；
- 旧 `maixcam/` YOLO11 姿态示例；
- `__pycache__/`、`.pyc`、PID/FIFO/WAV；
- 旧模型副本和本地构建产物。

原版本可从一代 Pro 的 `/root/surplife/app` 和远端 Git 历史追溯。

## 8. 验证记录

- Python `py_compile`：通过；
- Shell `sh -n`：通过；
- Pro 单元测试：19 项通过；
- MaixCAM2 关键词测试：6 项通过；
- STM32 协议测试：6 项通过；
- STM32 固件已烧录并读回 SHA256 校验；
- TIM2/TIM4 CEN 已置位、CCR 全 0；
- 完整 15 路灯牌 SPI 联调仍待现场完成。
