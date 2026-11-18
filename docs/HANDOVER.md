# 设备上手与交接说明

生成时间：2026-11-18  
适用设备：`maixcam-4e47`（LicheeRv Nano / SG2002，riscv64 Linux，Buildroot 2023.11.2）  
适用场景：本设备计划在 **−20 °C 冷室**环境下做灯牌 + 加热片温控实验，由其他人接手调试。

---

## 0. 一句话上手

**不需要 WSL、不需要 WireGuard、不需要任何隧道**：把设备连上任意 WiFi，
浏览器打开 `http://<设备IP>/` 就是 Web 控制台，`ssh root@<设备IP>` 就进系统（密码 `root`，**请先改**）。
换手机热点/换路由器**不用改代码**——WiFi 凭据是板上两个文本文件，PC 上直接改即可（见 §3）。

## 1. 拉代码 + 部署到设备

```bash
git clone https://github.com/Mola-maker/EnlightenJLU.git
cd EnlightenJLU/maixcam/pro/test
```

部署到设备（把 `<IP>` 换成设备实际地址，见 §4）：

```bash
scp -r . root@<IP>:/root/surplife/test/
scp tools/S99surplife      root@<IP>:/etc/init.d/S99surplife
scp tools/S99wifi-retry    root@<IP>:/etc/init.d/S99wifi-retry
ssh root@<IP> 'chmod 755 /etc/init.d/S99surplife /etc/init.d/S99wifi-retry'
ssh root@<IP> '/etc/init.d/S99surplife restart'      # 起 Web + KWS
```

板上目录与仓库一一对应：`/root/surplife/test/` ⇔ `maixcam/pro/test/`。

## 2. 服务构成与互斥关系（最容易踩的坑）

| 进程 | 作用 | 是否持有 SPI4 |
|---|---|---|
| `tools/spi_service.py --web-port 80` | **SPI4 唯一持有者**：13 路 PWM、温控、Web 控制台、接收 KWS 触发 | ✅ 独占 |
| `tools/thermal_pid.py` | 命令行温控 PID（调参用） | ✅ 独占 |
| `tools/temp_monitor.py` | 每秒打印 SG2002 结温 | ❌ |
| `main.py --no-spi` | KWS 语音识别，输出关键词行喂给 spi_service | ❌ |

**互斥**：Web 与 `thermal_pid.py` 都独占 SPI4（锁文件 `/tmp/stm32-spi4.lock`），同时跑会报
`RuntimeError: SPI bus 4 is already owned by another process`。

```sh
/etc/init.d/S99surplife stop      # 跑命令行 PID 前先停 Web
python3 -u tools/thermal_pid.py --setpoint 43 --margin 3 --enable \
        --max-duty 600 --hold-duty 170 --split-heat --count 1800
/etc/init.d/S99surplife start     # 跑完再起 Web
```

**注意**：`/etc/init.d/S99surplife start` 会执行 `usb_mic_mode.sh host`，把 USB-C 从 device 切成 host
（为了 USB 麦克风）。如果只需要 Web、不想动 USB 角色，用 `/root/surplife/start_web_only.sh`
（本仓库不包含该文件，内容见 `maixcam/pro/test/README.md` 的 Web 一节，即无麦克风分支的等效命令）。

## 3. 换 WiFi / 换网络（IP 不一样也不影响）

板上 WiFi 由 `/etc/init.d/S99wifi-retry` 负责，凭据来源：

1. 优先 `/etc/wifi.ssid` + `/etc/wifi.pass`
2. 缺失时回退 `/boot/wifi.ssid` + `/boot/wifi.pass`

**`/boot` 是 FAT 分区，可以直接把 TF 卡/启动分区挂到 Windows 上编辑**（纯文本，无引号）：

```text
/boot/wifi.ssid  →  写 SSID，例如 MyPhone
/boot/wifi.pass  →  写密码；**留空 = 开放网络**（key_mgmt=NONE）
```

放回设备开机即自动连接；也可在板上执行 `/etc/init.d/S99wifi-retry start` 立即重连。

> 本次修复：原脚本只会用 `wpa_passphrase` 生成 WPA-PSK 配置，**连不了开放 SSID**；
> 现在密码为空时按开放网络配置。原文件备份在 `/root/surplife/archive/debug-net/S99wifi-retry.orig`。

## 4. 设备 IP 怎么找（DHCP 分配，可能每天都在变）

按顺序试：

1. **mDNS**（板上 `avahi-daemon` 常驻）：
   - 浏览器 `http://maixcam-4e47.local/`
   - `ssh root@maixcam-4e47.local`
   - Windows 若解析不了 `.local`，装 Bonjour 或直接看第 2 条
2. **设备屏幕**：MaixApp 主界面会显示当前网络信息
3. **串口控制台**：接 UART，登录后 `ip -brief addr show wlan0`
4. **PC 侧 ARP 表**（设备 MAC 前缀 `38:7a:cc`）：`arp -a | findstr 38-7a-cc`

Web 端口优先 80，被占用时自动退到 8080。健康检查：

```bash
curl http://<IP>/api/health          # {"ok":true,"port":80}
curl http://<IP>/api/state           # 灯牌/温度/加热实时快照
curl -N http://<IP>/api/stream       # SSE 推流（页面用）
```

## 5. −20 °C 冷室调试注意事项

常温实测参数**不能直接照搬到冷室**，以下几点需要按现场重新标定：

1. **只需填两个物理量**：环境温度 `ambient_c` 和"加热全关时的结温"
   `no_heat_junction_c`。前馈占空比、预热带和 PI 增益由设备自动推导
   （`thermal_tuning.py`），**不需要也不应该在网页里调 Kp/Ki/Kd**。
   ```bash
   # 先在冷室里量出无加热结温（加热全关，等结温稳定）
   curl -s http://<IP>/api/state | python3 -c 'import json,sys;print(json.load(sys.stdin)["thermal"]["temp_c"])'

   # 填入环境温度和量到的无加热结温（enabled 先保持 false）
   curl -X POST http://<IP>/api/thermal -H 'Content-Type: application/json' \
     -d '{"ambient_c":-20,"no_heat_junction_c":5,"setpoint_c":43,"margin_c":3,"max_duty":1000,"enabled":false}'

   # 看推导结果：前馈占空比 / Kp / Ki / 预热带 / 满功率可达温度 / 警告
   curl -s http://<IP>/api/thermal
   ```
2. **加热能力**：`max_duty` 是硬件上限，不是控制参数。若 `/api/thermal` 返回
   `"reachable": false` 并提示"目标不可达"，说明 `max_duty` 给的功率在冷室里不够，
   需要提高 `max_duty`（并确认供电能力，5V/2A 用 `--split-heat`）或降低目标温度。
   示例：-20 °C 环境、无加热结温 5 °C 时，`max_duty=500` 预计只能到约 36 °C，
   `max_duty=1000` 预计 68 °C。
3. **结温读数**：SG2002 自带温度传感器读的是 SoC 结温，冷室里的热耦合关系与常温不同，
   先用 `tools/temp_monitor.py` 记录一段基线。
4. **结霜与凝露**：从冷室拿出设备时先断电、回温干燥，避免板面结露上电。
5. **超温/失效保护**：STM32 侧仍然生效（未配置、温度无效/过期、过温、链路超时都会强制 HEAT=0），
   但不要依赖它做唯一保护，冷室内建议留人值守并记录日志。

## 6. 校园网实测结论（省得重复踩）

| 场景 | 结论 |
|---|---|
| 设备与 PC 在同一手机热点 | ✅ 正常互访，推荐 |
| 设备连 `JLU.PC`（开放式） | ⚠️ 能拿到 `10.86.x.x/18`、能 ping 网关，但**客户端隔离**导致 PC↔设备不通；且**门户未认证**，设备上不了外网 |
| `eduroam` / `JLU.WIFI` | ⚠️ 需要 802.1X 账号，同样有客户端隔离风险 |
| 普通家用路由器 | ✅ 一般无客户端隔离，正常 |

## 7. 本仓库**不包含**的临时调试设施

2026-11-17/18 为了在"PC 在校园网、设备在手机热点"的跨网条件下远程调试，曾在**单台开发机**上额外搭建：

- WSL2 里的 WireGuard hub（`wg0 10.99.0.1`、用户态 `wireguard-go`、NAT 出口）
- 板子上的 `S99wireguard` + `wg-supervisor.sh` + `wg-pick-endpoint.sh` + `/etc/wireguard/wg0.conf`
- 把板子 USB-NCM 网口经 `usbipd` 挂进 WSL、并转发到 `127.0.0.1:8081` 供浏览器访问

**这些都不是项目的组成部分，对设备功能零依赖**，接手时应当清理掉，否则板子开机会把默认路由
指向一个不存在的 WireGuard 端点，表现为"能连 WiFi 但上不了网"：

```sh
mkdir -p /root/surplife/archive/debug-net
mv /etc/init.d/S99wireguard /usr/sbin/wg-supervisor.sh /usr/sbin/wg-pick-endpoint.sh \
   /etc/wireguard /root/surplife/waiter.sh /root/surplife/archive/debug-net/ 2>/dev/null
kill $(ps aux | grep -E '[w]g-supervisor|[w]ireguard-go' | awk '{print $1}') 2>/dev/null
wg-quick down wg0 2>/dev/null
```

## 8. 安全与维护

- `root` 密码目前是 `root`，接手前务必 `passwd`。
- 日志：`/root/surplife/run/web_service.log`、`/root/surplife/run/kws_web.log`、`/etc/wifi-retry.log`。
- `/var/log` 是 tmpfs（重启即丢），需要留存的日志请写 `/root/` 下。
- 固件烧录（STM32）：`st-flash --reset write firmware/PWM-Heater-web.bin 0x08000000`。

## 9. 相关文档

- `README.md`：系统架构、目录结构、当前功能与限制
- `docs/ASSET_INVENTORY.md`：资产来源与迁移记录
- `docs/THERMAL_PID_BRINGUP.md`：温控 PID 调试记录
- `maixcam/pro/test/README.md`：板上服务、SPI 协议、Web API、KWS 流程的详细说明
