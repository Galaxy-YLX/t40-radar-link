# T40 雷达数据链路

DJI T40 植保机 **RD2484R** 雷达的完整数据链路实现：一块 XCZU3EG（FZ3B）板卡同时接雷达的 CAN
总线和雷达自己的 WiFi，把控制与感知数据经以太网/WiFi 转发给电脑，由上位机实时显示点云并控制转速。

雷达的 0x00F0 感知报文格式是**逆向**出来的——固件里没有文档，报文里没有自描述。
完整的过程、证据和复现方法整理成了一个可复用的 skill：[`skills/t40-radar-reverse/`](skills/t40-radar-reverse/)。

> 本项目与 DJI 无关，全部结论来自对自有硬件上运行固件的观察与静态分析，仅供互操作研究。
> 逆向得到的字段语义见下方「0x00F0 记录格式」，方法论见 skill。

```
雷达 Type-C ──CAN(1Mbps)──┐
   └──────USB(AR1021x AP)─┤
                          ├── 板卡 XCZU3EG ──WiFi "t40_link"── 电脑
        MT7612U (给电脑) ──┘       192.168.9.1        192.168.9.10x
```

## 目录

```
protocol/PROTOCOL.md        板卡 ↔ 电脑 的 TCP 转发协议（帧格式、消息类型、命令）
bridge/t40_bridge.py        板卡侧桥接程序（CAN + 雷达 UDP → TCP，命令翻译回 CAN/UDP）
host/t40_cloud_view.py      0x00F0 点云实时显示（PPI 俯视图，四种数据源）
host/t40_gui.py             上位机控制台（PySide6 + pyqtgraph：转速/CAN/链路/延迟）
host/radar_min.py           板卡侧最小雷达客户端（flight_pos 门控 + CAN 桥接）
host/duml_v2.py             DUML v2 / 1E61 感知包解析
host/cloud_view_live.bat    Windows 双击启动点云显示
setup/start_all.sh          板卡上电一键恢复整套链路（AP + CAN + 桥接 + 看门狗）
setup/radar_bringup.sh      雷达侧后台看护（等网卡 → 起 AP → 等关联 → 起 radar_min）
setup/radar_usb_watch.sh    雷达网卡掉线后自动重新枚举 USB
setup/radar_watchdog.sh     雷达链路看门狗（收不到包 → 软恢复 → 重启板卡）
setup/hw_wdt.py             Zynq 片内看门狗守护（用户态卡死就硬复位/软重启）
setup/duml_can.py           板卡侧 CAN/DUML 工具（调转速、听遥测）
setup/hostapd_*.conf        两块网卡的 AP 模板配置
setup/t40-*.service         systemd 单元（链路、看门狗、硬件看门狗）
setup/autostart/*.sh        丢进 /root/autostart/ 即开机自启
skills/t40-radar-reverse/   逆向过程与结论（SKILL.md + 四份详细报告）
```

板卡侧代码全部是**纯标准库**，板上 Python 3.6 就能跑（不要用 3.8+ 语法）。
电脑侧需要 `pip install -r host/requirements.txt`（numpy / PySide6 / pyqtgraph）。

## 硬件连接

```
雷达 Type-C (15V 供电)
  ├── CAN_H/CAN_L  → 板卡 44 针扩展口 (PS CAN1 = can0, 1 Mbps)
  └── USB D+/D-    → 板卡 USB hub  (雷达自己的 AR1021x，作为 AP 网卡)

板卡 MT7612U ──WiFi "t40_link" (2.4G ch6)── 电脑
     192.168.9.1                            192.168.9.10~100 (DHCP)
```

> Type-C **区分正反面**，插反了 USB 不枚举。金属外壳必须装回，否则 USB 极不稳定。
> 雷达是 **STA（客户端）**，板子是 **AP**：雷达上电后自己扫描并连到 SSID `radar_wifi` 的开放热点。

## 快速启动

**板卡侧**——把 `setup/` 下的东西拷到 `/root/`（文件名保持原样，脚本里写死了路径）：

```bash
cp bridge/t40_bridge.py host/radar_min.py \
   setup/start_all.sh setup/radar_bringup.sh setup/radar_usb_watch.sh \
   setup/radar_watchdog.sh setup/hw_wdt.py setup/duml_can.py /root/
cp setup/hostapd_radar.conf /root/ap_1021.conf
cp setup/hostapd_t40link.conf /root/hostapd_t40link.conf
mkdir -p /root/autostart && cp setup/autostart/*.sh /root/autostart/
cp setup/t40-*.service /etc/systemd/system/
systemctl enable --now t40-link.service t40-radarwd.service t40-hwwdt.service
```

两个 hostapd 模板里的 `interface=CHANGEME` 要换成实际网卡名（`ip link` 看 `wlx*`），
`hostapd_t40link.conf` 的 `wpa_passphrase` 也要改。网卡名由 USB 网卡 MAC 决定，
换板子/换网卡时 `setup/start_all.sh` 顶部的 `IF_RADAR` / `IF_LINK` 一起改，
桥接的 `--wifi` 参数同理。

**前置条件（仓库里不含，得自己准备）**：

- **ath6kl 驱动**：雷达的 AR1021x（`0cf3:1022`）只有 ath6kl 支持，而板卡的厂商内核
  （4.14）里没有。需要把 `ath6kl_core.ko` / `ath6kl_usb.ko` 编出来放进 `/root/`
  （脚本按这个路径 insmod）；另外 MT7612U 需要内核带 `mt76x2u`。
- **CAN**：内核要有 `xilinx_can`，`can0` 对应 PS CAN1（44 针扩展口）。
- **`usb_modeswitch`**：MT7612U 出厂是 ZeroCD 模式（`0e8d:2870`），要切到 `0e8d:7612`。
- **`hostapd` / `dnsmasq` / `iw`**：`apt install hostapd dnsmasq iw usb-modeswitch`。

手动起一次（不装 systemd 时）：

```bash
# 1. 起雷达 AP（必须 ap_max_inactivity=600，否则雷达反复掉线）
ip link set wlx00037f2044d2 up
ip addr add 192.168.8.1/24 dev wlx00037f2044d2
hostapd -B /root/ap_1021.conf

# 2. 起桥接
python3 /root/t40_bridge.py

# 或者一步到位（含 CAN、电脑链路 AP、dnsmasq、看门狗）
sh /root/start_all.sh
```

**电脑侧**：

```bash
pip install -r host/requirements.txt
python host/t40_gui.py --host 192.168.9.1 --auto      # 控制台
python host/t40_cloud_view.py                          # 点云（默认走板卡转发）
```

## 点云实时显示

`host/t40_cloud_view.py` 把 0x00F0 的记录实时解成点云画成俯视 PPI（0° = 雷达正前方）。
**画面朝向默认是「整幅画面逆时针 180° + 方位逆时针」**：点云、121° 无数据扇区、
前/后/左/右 标注一起转，所以 0°（正前方）默认落在画面下方；面板「旋转」改回 0°、
勾掉「方位逆时针」就是老样子（0° 朝上、顺时针）。命令行走 `--rot 0 --cw`。
四种数据源任选：

```bash
# 直接运行就是走板卡转发（默认 tcp://192.168.9.1:46000）
python host/t40_cloud_view.py

# 直连雷达 UDP（电脑在雷达 AP 网段时；板卡上直接跑也行）
python host/t40_cloud_view.py --src udp://0.0.0.0:40204

# 回放自己抓的 pcap（2 倍速、循环）
python host/t40_cloud_view.py --src pcap:capture.pcap --speed 2

# 没有硬件时用合成数据看界面
python host/t40_cloud_view.py --src demo

# 不开界面，只验解码链路
python host/t40_cloud_view.py --selftest capture.pcap
```

界面直读的量：帧率、点/秒、本帧点数、累积圈数、c0 序号、**头校验和是否通过**、
**c5 反推的实测转速**、板卡 STATUS（uptime/CAN/radar_pkts/流量/dropped/wifi）。
可调：量程（2–70 m）、幅度阈值 c3、累积圈数（0 = 只看当前圈）、画面旋转角度
（0–359° 逆时针，默认 180°，带 0/90/180/270 快捷键）、方位顺/逆时针（默认逆时针）、
121° 无数据扇区示意；**雷达转速**（300–1800 rpm，走 CMD 0x40 → CAN 0x1E80）与
马达使能/停转。改朝向会清空已累积的历史点（旧点按老朝向算好了像素坐标，重投影不了）。

**自愈与报错**

- TCP 断线**自动重连**（1→5 s 退避）：板卡比电脑晚起来也能自己接上；
- 顶部红/黄/绿状态条：连不上 / 连上没数据 / 正常，一眼看出卡在哪一层；
- 看门狗：连上却 >3 s 没有 0x00F0，会直接说"雷达没在出点"，并附板卡 radar_pkts 与
  wifi_connected，区分"雷达没上电"和"雷达 WiFi 没连"；
- 看门狗：连上 **5 s 收不到 STATUS** 就直接报"板卡侧桥接卡住了（典型是 ath6kl 楔死）
  → 给板卡断电重启"。以前这种情况只会显示"等待第一帧 0x00F0"，把"板卡楔死"误判成
  "雷达没出点"；
- 所有异常写 `t40_cloud_view.log`；pythonw 没有控制台，致命错误会弹窗，不会静默起不来。

**绘图性能（修过"量程一大就卡死"）**

原因是历史点每来一帧就把**全部**点塞进一个 ScatterPlotItem 重画（pyqtgraph 的
`setData` 是 O(全量)，10 万点 130 ms/帧），而量程一大所有点又都落进视野。量程 70 m +
累积圈数 400 时 GUI 线程被占满：实测事件循环平均延迟 **282 ms**、最长 **511 ms**。
现在的做法：

- 历史点**分块**追加，每帧只重画最后一块（≤ `CHUNK_PTS` 点，实测 3 ms/帧）；
- 改量程/阈值只重画网格，不再连带重画历史（量程 40→70 同步耗时 13.8 ms）；
- 历史点总量封顶 `MAX_HIST_PTS = 80000`（≈15 ms 绘制），超了整块丢最老的；
- 重画限速 40 Hz：数据源再快也只按这个节奏刷。

同样的压力场景现在是平均 10.8 ms、最长 34.7 ms，拖动/点按钮全程跟手。信息栏多了
"累积点数 x / 80000"和"界面耗时 x.x ms/帧"两行。

> 解码吞吐实测约 **1 万帧/s、270 万点/s**，远高于 1200 rpm 下的 20 帧/s，所以瓶颈
> 只会是网络和绘图，不会是这个解析器。

界面布局：

```
┌──────────────────────────────────────────┬─────────────────┐
│            前 0°（盲区扇区，灰）          │ 数据源 pcap …   │
│         ╱⌒⌒⌒⌒⌒⌒⌒⌒╲                      │ 量程 8.00 m     │
│   左270°│  1 2 3 4 m  │右90°              │ 阈值 c3 80      │
│         │    ●雷达   │                   │ 累积 30 圈      │
│         ╲＿＿＿＿＿＿╱                     │ 帧率/点率/校验和 │
│            后 180°（走廊纵深）             │ 转速 1236 rpm   │
└──────────────────────────────────────────┴─────────────────┘
```

## 关键参数

| 项目 | 值 |
|---|---|
| 雷达 AP | SSID `radar_wifi`、ch48 / 5240MHz、**802.11a legacy 20MHz 无 HT**、开放 |
| 地址 | 板卡（雷达侧）`192.168.8.1`，雷达 `192.168.8.2`；板卡（电脑侧）`192.168.9.1` |
| 雷达业务 | `udp/40204`（感知数据，雷达→主机）、`udp/50007`（控制） |
| 雷达 SSH | `ssh -p 107 -oHostKeyAlgorithms=+ssh-dss root@192.168.8.2`（root/root） |
| CAN | 1 Mbps，ESC→主机 ID `0x0A3`，主机→ESC `0x0A2` |
| 转发 | TCP `46000` |
| 默认转速 | **1200 rpm**（`start_all.sh` 里的 `RPM=`，上位机可改） |

## 转发协议

板卡与电脑之间只跑一条 TCP（`46000`），帧格式 24 字节头 + payload + CRC32，
消息类型覆盖 HELLO / HEARTBEAT / CAN_FRAME / RADAR_UDP / STATUS / CMD / CMD_ACK / LOG。
完整定义见 [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md)。

设计上刻意把转发层和实现解耦——以后把桥接挪到 PL（AXI-Stream/DMA）时上位机不用改；
时间戳统一用板卡 `CLOCK_MONOTONIC`，避免两端时钟差；未知消息类型必须跳过而不是断连。

## 桥接侧的几个坑（都踩过）

- **发送不能和 recv 串在一起**：老代码把"排空发送队列"写在 `conn.recv(timeout=0.5)` 之后，
  而上位机只发 1Hz 心跳，于是每个雷达包都要在队列里干等到下一次 recv 超时——实测 **80%
  的帧被拖了 100 ms 以上**，p99 495 ms。现在收、发各一个线程，p50 1 ms / p99 54 ms。
- **状态采集必须脱离客户端线程**：ath6kl 楔死时 `iw`/sysfs 调用永不返回，旧桥接把客户端
  线程钉死在 `build_status()` 里，上位机连 STATUS 都收不到。现在采集在后台线程 + 缓存快照，
  STATUS 每秒照发，末尾的 `status_age` 字段把"已经采不出新状态"这件事说出来。
- **不要调 `iw`**：AP 模式本来也拿不到 RSSI，rx 计数改读 `/proc/net/dev`；`ip` 调用用
  Popen + 轮询 + 超时只 kill 不 wait（D 状态子进程 `wait` 会永久阻塞）。
- **单客户端**：新连接会踢掉旧连接。窗口开着的时候跑诊断脚本会互相抢。

## 延迟与刷新率

一次「雷达扫到 → 界面画出来」要过四段，2026-11-02 实测：

| 段 | 改前 | 改后 | 怎么改的 |
|---|---|---|---|
| 雷达→板卡（WiFi） | 未单独测 | 同左 | 雷达没有发送时间戳，只能靠出帧率间接看 |
| 板卡桥接排队 | **251 ms（p99 495）** | **1 ms（p99 54）** | 发送线程与收命令解耦 |
| 板卡→电脑（WiFi） | p50 ~1ms / p90 10~35ms | 同左 | `t40_link` 2.4G，抖动随环境 |
| 界面刷新 | 200 ms/次 | 33 ms/次 | `QTimer` 200→33 |

**转速是另一个杠杆**——雷达的出帧率基本跟转速线性走，但每圈点数在 900 附近见顶：

| rpm | UDP/s | 1E61/s | 点数/s | 点/圈 | 转一圈 |
|---|---|---|---|---|---|
| 300 | 21.8 | 19.1 | 12 | 2.3 | 189 ms |
| 600 | 27.1 | 21.8 | 31 | 3.2 | 102 ms |
| 900 | 49.9 | 34.5 | **284** | **19.1** | 67 ms |
| 1200 | 64.8 | 44.4 | 252 | 12.7 | 50 ms |
| 1500 | 80.9 | 55.4 | 183 | 7.3 | 40 ms |
| 1800 | 97.1 | **66.5** | 160 | 5.3 | **33 ms** |

1200 是拐点（帧率 +29%、点数 -11%），所以设成默认。想要点云更密就降到 900，
想要刷新最快就上 1800（点数受场景影响很大，换场景要复测）。

## 0x00F0 记录格式（从固件解出并逐帧验证）

19 字节头之后是 N×12 字节的记录，每条 6 个 u16（小端）：

| 字段 | 含义 | 单位 | 备注 |
|---|---|---|---|
| c0 | 方位角 | mrad | 旋转头角度，帧内随 c5 严格线性（corr=1.00），量化 268 格/圈 |
| c1 | 俯仰角 | mrad | 存的是 `(el + 2π)`，减 6283 得实际值；64 元虚拟阵 DBF 波束格 |
| c2 | 距离 | mm | u16 在 65.5 m 回绕；一格 39.7 mm（带宽 3.78 GHz） |
| c3 | 回波幅度 | 0.01 | 近场饱和、远场滚降 |
| c4 | 信噪比 | 0.01 | 与 c3 同形，比值随距离 0.85→0.3 |
| c5 | 探测时刻 | µs | 绕内时间偏移，帧内单调；对 c0 的斜率就是转速 |

容器头（19 字节）里的关键位置：偏移 3 是流水号 u16，**偏移 13 是记录区字节数 u16**，
偏移 17 / 18 是两个 8 位累加校验和（一个覆盖记录区、一个覆盖头自身）。它**不分片**，
一个 UDP 包就是一整帧。

固件里的来源：写入函数 `0x4CBF8`，读 A@`0x0C456850` / B@`0x0C473D10` / C@`0x0C4911D0`
三个 10000 条目的全局数组，`c0=A.x×1000`、`c1=(A.y+2π)×1000`、`c2=A.z×1000`、
`c3=B.x/100`、`c4=B.y/100`、`c5=C-count`。

解码实现见 `host/t40_cloud_view.py` 的 `decode_00f0()`；它是怎么被解出来的——被动抓包、
节拍分析、栈插桩、内存快照、1E61 交叉验证——写在 skill 里。

## DUML 速查

| msg_id | 含义 | 方向 |
|---|---|---|
| `0x1E0B` | IMU（payload[14:26] 三个 float32） | ESC→主机 |
| `0x1E0F` | ESC 状态（payload[29:31] = 转速 rpm） | ESC→主机 |
| `0x1E80` | 设转速（u16 LE） | 主机→ESC |
| `0x1E82` / `0x1E09` / `0x1E0A` | 停转 / 使能 / 禁用 | 主机→ESC |
| `0x1E21` | `flight_pos`（10 Hz 心跳，打开 1E61 门控） | 主机→雷达 |
| `0x0032` | 激活（含 SN） | ESC→主机 |

DUML v1（CAN 上）CRC：头 `CRC8(init 0x77, poly 0x8C 反射)` 覆盖前 3 字节；
尾 `CRC16(init 0x3692, poly 0x8408 反射)` 覆盖 `frame[:-2]`，小端。

`host/radar_min.py` 存在的意义：以 10 Hz 往 `192.168.8.2:50007` 发 `0x1E21 flight_pos`，
并把雷达与 ESC 之间的电机 DUML 双向转发。**没有这一步，雷达的 motor FSM 停在初始态，
1E61 永远是 69 字节空包**（0x00F0 的门控同理）。

## 稳定性设计

板子会「假死」：ath6kl Oops 之后 `iw`/sysfs 永不返回、雷达不再出包、sshd 连上不给
banner。这种状态软件救不回来，但可以**用软件触发重启**，不必人工断电。三层处理：

- **桥接自己**：状态采集后台化 + `status_age` 上报，让上位机知道"板卡侧采集卡住了"；
- **`setup/radar_watchdog.sh`**：雷达 AP 口 rx 连续 60 s 不涨先重启 AP 软恢复，25 s 后
  还没包就 `reboot`。为避免误伤：雷达没关联（没上电/被拔）只记日志；本次开机从没出过包
  时给足 15 分钟；30 分钟内重启过 3 次就停下等人工。dmesg 里有 ath6kl Oops 或
  `/tmp/t40_bridge_stat.ts` 超过 180 s 没更新 → 直接重启。
- **`setup/hw_wdt.py`**：接管 Zynq 片内 cadence 看门狗，喂狗条件是「桥接的状态采集最近
  N 秒成功过」而不是定时喂。⚠ 实测板卡上这两颗 SWDT 超时**并不拉复位**，所以它只是
  第一层，真正兜底的是它判死后的软重启（`reboot` → 25 s 后 sysrq-b）。

暂停任意一层：`touch /root/radar_watchdog.skip`、`touch /root/hw_wdt.skip`、
`touch /root/radar_usb_watch.skip`。日志都在 `/root/*.log`。

## 已知限制

- **雷达 STA 是被动扫描**：只听 beacon、不发 probe request。所以 AP 必须是它自己那颗
  AR1021x（MT7612U 起的同名热点它不认）。
- **RSSI 拿不到**：ath6kl 在 AP 模式下不实现 `station dump`，上位机显示 N/A。
- **NetworkManager 必须不管理 AP 网卡**，否则它的周期扫描会把雷达踢下线：
  `/etc/NetworkManager/conf.d/99-radar-ap.conf` 里写
  `unmanaged-devices=interface-name:wlx00037f2044d2`。
- **Zynq 片内看门狗不拉复位**（见上），真正的硬复位路由要去查 SWDT 的 CRL_APB 配置。
- **`radar_watchdog.sh` 里所有可能卡在 D 状态的命令都必须后台跑 + 轮询 + kill**，
  否则最需要它的时候它自己先挂住。

## 逆向过程

0x00F0 的格式、字段语义、单位标定不是猜出来的：被动抓包定节拍 → 容器层暴力扫描定
19 字节头 → 静态反汇编定位写入函数 → `LD_PRELOAD` 插桩抓调用栈 → `/proc/pid/mem`
内存快照复算 → 用另一路已知点云（1E61）交叉验证几何。每一步的判据、否掉的假设和
工具都在 [`skills/t40-radar-reverse/SKILL.md`](skills/t40-radar-reverse/SKILL.md)，
四份带数据的详细报告在它的 `references/` 下。

## 许可

MIT，见 [LICENSE](LICENSE)。第三方组件（PySide6 / pyqtgraph / numpy）遵循各自许可。
