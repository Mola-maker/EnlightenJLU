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

SPI 模式 0、8 bit，生产配置使用 **100 kHz**。200 kHz 在整机负载下会偶发
半帧；500 kHz 不建议使用。打开设备后等待 0.5 s，让软件 SPI4 稳定；每个
响应都会校验 magic、长度和 CRC，失败时最多重试 5 次，间隔 100 ms。

软件 SPI4 偶尔会在帧头前多送一个字节并丢掉末尾的保留 0，传输层会在
CRC 校验通过时自动去掉首字节并补零恢复。连续坏帧最终由 20 Hz 心跳和
HELLO 重建处理。

2026-11-08 的 Web 固件把 STM32 SPI1 改为 **软件 NSS**（`SSM=1, SSI=0`）。
总线上只有 STM32 一个从机时，即使 A24→PA4 的 CS 线松动，STM32 仍能完成
64 字节事务；A24/PA4 仍建议保留，方便示波器/逻辑分析仪观察。
ST-Link 不在本测试链路上；它只用于烧录 STM32。

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
nohup sh -c 'python3 -u main.py --no-spi --debug-kws 2>&1 |
  tee /root/surplife/run/kws_live.log |
  python3 -u tools/spi_service.py --hold 1.2 --frequency 100000 --web-port 80' \
  </dev/null >/root/surplife/run/spi_live.log 2>&1 &
```

Web 控制台：

```text
http://192.168.43.229/
```

服务优先监听 80；如果 80 已占用会自动使用 8080。新版页面以
`html_20261113_a00bec.html` 的浅色卡片/侧栏/曲线风格为标杆，去掉了
场景预设、固件预设、单灯调光等冗余动作，只保留两个焦点：

1. **灯牌波形 + 关键词状态**：13 路 PWM 热力波形、当前波形、
   关键词监听/触发/未接入状态、最后一次关键词和 hello/welcome 分数；
2. **加热波形 + 自动整定**：温度/HEAT duty 双曲线、40–46 °C 目标带、
   环境温度、无加热结温、自热温升、setpoint 43 °C、裕度 ±3 °C、前馈占空比，
   以及**只读**的自动 Kp/Ki、预热带、满功率可达温度和 P/I/D 实时项。

左侧栏是真正的分页导航，一次只显示当前功能：

1. 灯牌波形；
2. 加热波形；
3. 加热控制（环境温度 + 无加热结温 → 自动整定）；
4. 关键词识别；
5. 网络（改 WiFi 的 SSID/密码，设备自行重连）。

网络页是交接关键：使用者不需要登录 shell，也不必把 TF 卡插到电脑上，
在浏览器里填自己热点的 SSID/密码即可（`POST /api/wifi` 会同时更新
`/etc/wifi.ssid`、`/etc/wifi.pass` 与 `/etc/wifi.networks` 并触发
`S99wifi-retry`，旧文件留 `.bak`）。

**设备 IP 在网页上常显**，不用翻页也不用登 shell：

- 左侧栏底部：`IP 192.168.43.229 · Jl521522`（进页面拉一次，之后每 10 s 刷新）；
- 浏览器标签页标题：`192.168.43.229 · JLU Console`；
- 「网络」页：当前 SSID / 当前 IP / 已保存 SSID / 网络类型；
- 命令行：`curl http://<设备IP>/api/wifi`。

`/`、`/static/`、`/api/` 全部返回 `Cache-Control: no-store`，改完页面刷新即生效，
不会出现"改了却没变"的缓存错觉。

页面只保留灯效波形选择、整体亮度、关键词待机和加热使能/PID 参数应用；
不再提供预设动作按钮，也不再把四个功能同时堆在一页。

接口：

```text
GET  /api/state      lamp / kws / thermal / link 当前状态
GET  /api/history    lamp 12 s 波形 + thermal 5 min 波形
POST /api/lamp       effect / kws / off
GET  /api/thermal    当前自动整定结果（前馈占空比 / Kp / Ki / 预热带 / 可达性）
POST /api/thermal    ambient_c / no_heat_junction_c / setpoint_c / margin_c / max_duty / enabled
GET  /api/wifi       当前 SSID / IP / 已保存凭据 / 候选网络
POST /api/wifi       ssid + password（密码留空=开放网络）；写入后设备自行重连，当前连接会断开
```

示例：

```sh
curl -X POST http://192.168.43.229/api/lamp \
  -H 'Content-Type: application/json' \
  -d '{"command":"effect","effect":"comet","target":8191,"brightness":80}'

# 只填两个物理量：环境温度 + 无加热结温，其余由设备推导
curl -X POST http://192.168.43.229/api/thermal \
  -H 'Content-Type: application/json' \
  -d '{"ambient_c":-4,"no_heat_junction_c":21,"setpoint_c":43,"margin_c":3,"max_duty":500,"enabled":false}'

# -20 °C 冷室：无加热结温需现场实测后填入
curl -X POST http://192.168.43.229/api/thermal \
  -H 'Content-Type: application/json' \
  -d '{"ambient_c":-20,"no_heat_junction_c":5,"max_duty":1000,"enabled":false}'

# 换 WiFi（也可以直接在网页"网络"页填）
curl -X POST http://192.168.43.229/api/wifi \
  -H 'Content-Type: application/json' \
  -d '{"ssid":"MyPhone","password":"myhotspot123"}'
curl -s http://192.168.43.229/api/wifi          # 看当前连的/已保存的
```

默认加热是关闭的；勾选页面“加热使能”或 POST `enabled:true` 才会实际
下发 HEAT。未接入 USB 麦克风时页面会显示“KWS 未接入”，不会伪造监听状态。

设备已安装 `/etc/init.d/S99surplife`，开机自动切换 USB host、等待 USB
麦克风，然后启动 KWS + Web + SPI 服务；没有麦克风时退化为 Web-only。
手动重启：

```sh
/etc/init.d/S99surplife restart
```

停止时先停 SPI 服务，让它发送 `ALL_OFF`：

```sh
/etc/init.d/S99surplife stop
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

### 4.1.1 开机自动启动与黑屏恢复

设备上已安装 `/etc/init.d/S99surplife`：开机后切换 USB-C 到 host、等待
USB 麦克风（card 2），然后启动 KWS + `spi_service.py`；没有麦克风时退化为
Web-only，HTTP 仍可用。启动日志：

```text
/root/surplife/run/web_service.log
/root/surplife/run/kws_web.log
```

手动：`/etc/init.d/S99surplife start|stop|restart`。仓库内的安装副本为
`tools/S99surplife`：

```sh
scp tools/S99surplife root@192.168.43.229:/etc/init.d/S99surplife
ssh root@192.168.43.229 'chmod 755 /etc/init.d/S99surplife'
```

**不要**把 init 脚本备份命名成 `/etc/init.d/S06maixapp.bak` 等。Buildroot 的
`/etc/init.d/rcS` 使用 `for i in /etc/init.d/S??*`，任何 `S` + 两位数字开头
的备份文件都会被当成启动脚本执行。之前多个 `S06maixapp.*.bak` 导致多个
`launcher_daemon`/`launcher` 同时打开 `/dev/cvi-vpss`、`/dev/cvi-vo`，表现为
设置、相机、应用商店打开后只有背光/黑屏。备份统一放到：

```text
/root/surplife/init_backups/
```

同理，`S30wifi.disabled` 也会被 `S??*` 匹配；禁用脚本必须移出
`/etc/init.d`。正常启动应只有一个 `launcher_daemon` 和一个 `launcher`。
未接 USB 麦克风时 `/dev/input/event1` 通常是 `hyn_ts`；接上 USB 麦克风后
其 HID 可能占用 event1，`hyn_ts` 变为 event2。MaixPy 的
`maix.touchscreen.TouchScreen()` 会自动扫描并找到 event2，不要为了 event
编号在启动时重绑触摸。

### 4.1.2 温度命令行监控（PID 调试）

Web 控制台不再作为温度调试入口。先用独立命令行工具确认 SG2002 结温：

```sh
cd /root/surplife/test
python3 -u tools/temp_monitor.py --interval 1
python3 -u tools/temp_monitor.py --interval 0.5 --count 20
python3 -u tools/temp_monitor.py --format csv --count 20 > /tmp/temp.csv
python3 -u tools/temp_monitor.py --format json --count 5
```

输出示例：

```text
2026-11-11T03:27:57.813+00:00   57.031 °C  src=sysfs  min=56.681 max=57.031 avg=56.703  trend=+0.350 °C/s
```

默认读取：

```text
/sys/class/thermal/thermal_zone0/temp
```

`--source reg` 可退回直接读取 SG2002 TEMPSEN 寄存器；`--window` 控制滚动
min/max/avg 样本数。做 PID 前先停掉 Web/KWS 服务，避免服务负载本身改变
结温基线：

```sh
/etc/init.d/S99surplife stop
python3 -u tools/temp_monitor.py --interval 1
```

### 4.1.3 温控 PID（命令行）

PID 在 MaixCAM 命令行端计算，STM32 只执行 HEAT1/HEAT2 并做硬安全保护。
5V/2A 适配器建议用 `--max-duty 500 --hold-duty 350 --split-heat`；
5V/3A 以上电源可用 `--max-duty 600 --hold-duty 350`（默认 balanced）。
duty 是两路合计功率：balanced 两路同 duty，split 拆成 HEAT1=1000‰、
HEAT2=0/200‰，避免 5V 母线出现 3A 峰值：

### 4.2 温控参数由设备推导（推荐用法）

**操作者只需要提供两个能实测的物理量**，其余全部由设备计算：

| 输入 | 含义 |
|---|---|
| `ambient_c` | 测试环境温度（冷室 / 房间） |
| `no_heat_junction_c` | 加热全关时板子稳定后的结温（含 SoC 自热） |

设备据此推导**前馈占空比、预热带和 PI 增益**（`thermal_tuning.py`）：

```text
自热温升   self_heat = no_heat - ambient
所需升温   required  = setpoint - no_heat
前馈占空比 hold     = 1000 * required / g          （clamp 到 max_duty）
满功率上限 expected = no_heat + g * max_duty / 1000
Kp = tau / (K * (lambda + theta))          K = g / 1000 [°C/‰]
Ki = Kp / (lambda + theta)
Kd = 0
```

其中 `g = 63 °C`（1000‰ 加热相对"无加热基线"的稳态升温）、`tau = 180 s`、
`theta = 10 s`、`lambda = 35 s` 是**器件常数**，由 2026-11-17 的整定实验反推：
该次实验（环境 −4 °C、无加热结温 21 °C、hold 350‰ 稳住 43 °C）给出
`g = (43−21)/0.35 ≈ 63`；把同样输入喂给上面的公式得到 `hold=349‰、Kp=63.5、
Ki=1.41、预热带 3.3 °C`，与当时手工整定的 350‰/Kp=60/Ki≈1.0/4 °C 一致。
也就是说**自动整定是与实测标定自洽的，不是拍脑袋的经验值**。

命令行（推荐）：

```sh
cd /root/surplife/test

# dry-run：给出环境温度与无加热结温，设备算好参数只打印不加热
python3 -u tools/thermal_pid.py --ambient -4 --no-heat 21

# 实际加热（5V/2A 适配器 + split 通道）
python3 -u tools/thermal_pid.py --ambient -4 --no-heat 21 \
  --max-duty 500 --split-heat --enable

# -20 °C 冷室：无加热结温需现场实测。max_duty 500 通常不够
python3 -u tools/thermal_pid.py --ambient -20 --no-heat 5 \
  --max-duty 1000 --split-heat --enable
```

启动时会打印推导结果，例如：

```text
[tune] 环境 -20.0 °C / 无加热 5.0 °C -> 自热 25.0 °C, 前馈 603‰ (hold 603‰),
       Kp 63.5, Ki 1.410, 预热带 5.7 °C, 满功率最高约 68.0 °C
```

手动给 `--kp/--ki/--kd/--hold-duty/--warmup-band` 仍然生效（工程整定用），
显式给出的值优先于推导值；不传 `--ambient/--no-heat` 时行为与旧版一致。

### 4.2.1 旧的"手工给 Kp/Ki"用法（仅台架整定）

```sh
# 调参：Kp/Ki 单位为 permille/°C 与 permille/(°C*s)
python3 -u tools/thermal_pid.py --setpoint 43 --margin 3 \
  --kp 60 --ki 1.0 --kd 0 --enable \
  --max-duty 500 --hold-duty 350 --warmup-band 4.0
```

### SPI 稳定性：为什么要限制事务频率

SPI4 是**内核态 bit-bang**，一帧 64 字节在 100 kHz 下占用约 5 ms 的严格时序。
`spi_service.py` 的主循环每圈都会发一整帧，所以循环快慢直接等于总线占用率：

| 轮询频率 | 单核 CPU | 现象 |
|---|---|---|
| 50 Hz（旧行为，`select` 20 ms 超时后立即发帧） | ~39%，0% idle | 约 40–60% 的秒里出现 `FAULT_SPI(0x4)`，偶发 `LINK_LOST(0x400)+NEED_HELLO(0x10)` → 网页在"已连接/连接中断"之间跳变 |
| 20 Hz（现在的默认 `--poll-hz 20`） | ~25%，66% idle | 110 秒观测内 `link=3` 0 次、`transaction failed` 0 次；仅剩被重试吸收的瞬时 `0x4`（前端不显示） |

配套改动：网页轮询从 250 ms/1 s 降到 **1 s/3 s**（`/api/state` 要序列化整份状态，
`/api/history` 要序列化数分钟波形，单核上 4 Hz 会把 CPU 吃满）。

调法：

```sh
python3 -u tools/spi_service.py --web-port 80 --poll-hz 10   # 更省，灯效动画略粗糙
```

另外，板上 `/maixapp/apps/launcher`（厂商桌面 UI）常驻占 **39% CPU + 约 90 MB**，
做温控/灯效实验时建议停掉（它由 `/etc/init.d/S06maixapp` 启动，下次开机会自己回来）。

默认策略：

- **PI 为主，Kd=0**：SG2002 结温约 1.048 s 更新一次，且存在量化噪声；
  热对象时间常数长，D 项通常放大噪声，先不启用；
- **暖启动**：离目标超过 `--warmup-band` 时用 `--max-duty` 开环加热，
  积分器固定在 `--hold-duty`，不会把饱和输出累积进积分；进入目标前
  4 °C 后从保持占空比开始 PI；
- **预测刹车**：P 项使用最近 45 s 的线性拟合斜率和 90 s 预测温度，
  在 SoC 传感器到达 43 °C 前提前减小占空比，避免长滞后造成的 3–4 °C
  过冲；可用 `--slope-window`/`--slope-lookahead` 调整；
- 进入 PI 后仍使用条件积分：输出已经饱和时冻结积分，避免再次过冲；
- 目标 43 °C，`--margin 3` 建立 46 °C 硬上限；回落到 45 °C 以下才恢复；
- `--max-duty` 默认 500（50%），`--hold-duty` 默认 350（35%）；
- 不带 `--split-heat` 时两路同 duty，600‰ 的 5V 峰值约 3A，需要 5V/3A+；
  5V/2A 必须加 `--split-heat`，此时 600‰ 为 HEAT1=1000‰、HEAT2=200‰，
  500‰ 为 1000/0；
- 若 duty 回读一直 600/500 但温度反降，说明电源或电阻组进入热保护，
  不能靠继续调 PID 解决。

日志中的 `up=` 是 STM32 运行秒数，`rst=` 是检测到的复位次数，
`fault=0x....` 是 STATUS 故障位，`link=DOWN` 表示最后一次 SPI 事务已失败，
不再沿用上一帧的 `link=2/heat=599`。若长时间运行时出现 `rst` 增加或
`link=DOWN`，应先检查 5 V 电源余量、共地、SPI 线和加热器回流路径，
而不是继续调 PID。

固件侧保护：

- 未配置、未使能、温度无效/过期、`setpoint+margin` 过温、SPI 链路丢失
  都会强制 HEAT=0；
- STATUS 返回 `heater_duty`、`heater_flags`、STM32 uptime；
- `ALL_OFF` 会关闭 HEAT 并要求重新配置；
- 主机在链路恢复/STM32 复位后会在同一帧重发 HELLO + THERMAL_CONFIG，
  并在连续坏帧后重新打开 SPI 设备。

2026-11-17 实测：`--max-duty 600` 时结温可以从约 35 °C 升到 43 °C；
但第二次运行约 29 s 后 STM32 状态变为 `flags=0x00`，随后 SPI 连续
`bad magic`。后续定位到 S99 残留 supervisor 可能导致第二个 SPI owner，
以及 5V/2A 适配器在两组同时 PWM 时承受 3A 峰值。现在已加 SPI 锁和
HEAT1/HEAT2 通道模式。2026-11-18 split 600‰ 连续 10 分钟结温从
约 19 °C 升到约 39 °C；但随后多次出现 duty 回读不变、温度反降，
定位为 5V 适配器/加热支路进入保护，需稳定电源或间歇加热。

### 4.2 USB 麦克风检查

USB 麦克风当前为 ALSA card 2：

```sh
arecord -l
arecord -D plughw:2,0 -f S16_LE -r 16000 -c 1 -d 5 /tmp/mic.wav
amixer -c 2 sget "Mic"
```

`config.json` 中的 `mic_gain` / `mic_agc` 会在 KWS 启动前设置增益和 AGC。
可用 `--debug-kws` 打印每一帧的关键词概率：

```sh
python3 -u main.py --no-spi --debug-kws
```

注意：

- 模型是中文拼音 KWS，关键词为 `ha1 lou2` / `wei1 er2 kang1`；
- 请说中文“哈喽”，不要读英文 `hello`；
- 只有在模式为 `KWS 待机` 时关键词才驱动 GATE/RELEASE；网页手动灯效模式会暂停关键词动作；
- 如果概率一直为 `0.000`，先用 `wav/hallo_zh_16k_pad.wav` 验证模型，再检查麦克风距离、增益和发音。

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
firmware/PWM-Heater-test.hex        # GLYPH_ALL_LAMPS + [temp]
firmware/PWM-Heater-test.bin
firmware/PWM-Heater-web.hex         # Web 直控：SET_LAMPS，13 路亮度
firmware/PWM-Heater-web.bin
firmware/PWM-Heater-v1-base.hex      # 旧：已验证基线
firmware/PWM-Heater-v1-base.bin
```

测试固件 SHA256：

```text
PWM-Heater-test.bin
5fcb600380c41cd2619e35c9e5158ee10e6a86e7c56c53df17ff6707ff81ca78

PWM-Heater-web.bin
4b5cd2eeb31c186e9b9743a252ab6d41e7af2bd6326cbd6c66cd83a09485fc59
```

烧录命令（在 ST-Link 所在主机上执行）：

```sh
st-flash --reset write firmware/PWM-Heater-test.bin 0x08000000
```

必须用 ST-Link 烧录 `PWM-Heater-test.bin` 到 STM32 后，SPI 才会返回有效
STATUS。网页任意亮度控制还需要烧录 `PWM-Heater-web.bin`：

```sh
st-flash --reset write firmware/PWM-Heater-web.bin 0x08000000
```

烧录后 CAPS features 应为 `0x3F`（含 `DIRECT` 和 `THERMAL_PID`），网页会隐藏“待升级”提示，
亮度滑杆和 13 路灯效才会直接控制真实 PWM。

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
