# T40 雷达数据链路协议（板卡 ↔ 电脑）

**版本**：1.0
**日期**：2026-11-02
**传输**：TCP 单连接，默认端口 **46000**
**实现**：板卡 Linux 用户态桥接程序（`bridge/t40_bridge.py`）↔ 上位机（`host/t40_gui.py`）

---

## 1. 总体架构

```
雷达 ESC ──CAN (1 Mbps, can0)──┐
                               ├──► t40_bridge.py ──TCP 46000──► wlx… (t40_link 192.168.9.1) ──WiFi──► 上位机
雷达上部 ──UDP 40204 / 50007───┘         ▲                                                        (PySide6
   (AR1021x AP, 192.168.8.1)             └── 命令翻译成 CAN 帧 / UDP 包 ──► 雷达                    +pyqtgraph)
```

板卡侧桥接程序是**唯一**同时接触 CAN 和雷达 UDP 的进程，它把两类数据统一成一种帧格式转发给电脑；电脑发来的命令也由它翻译成 CAN 帧或 UDP 包。

> 早期方案走网线（板卡 `eth0` = `192.168.1.254`），现在默认走板卡上的第二块网卡
> MT7612U 起的 `t40_link` 热点（`192.168.9.1`）。协议本身与物理链路无关，换回网线不用改任何帧格式。

**设计原则**

- 协议与实现解耦：以后把转发层挪到 PL（AXI-Stream/DMA）时，上位机不需要改。
- 板卡时钟为准：所有时间戳由板卡打，避免电脑和板卡的时钟差。
- 未知类型必须忽略，不能断开连接（向前兼容）。

---

## 2. 帧格式

所有多字节整数一律 **小端（LE）**。

```
偏移   长度   字段        说明
0      4     magic       0x54 0x34 0x30 0x52  ("T40R")
4      1     version     协议版本，当前 0x01
5      1     type        消息类型，见第 3 节
6      2     flags       保留，填 0
8      4     seq         帧序号 uint32，每帧 +1，回绕
12     8     ts_ns       板卡 CLOCK_MONOTONIC，uint64，单位纳秒
20     4     length      payload 字节数 uint32
24     N     payload     载荷
24+N   4     crc32       CRC32/IEEE，覆盖字节 4 ~ (24+N-1)，uint32
```

- 固定头 **24 字节**，尾部 **4 字节 CRC**。
- `length` 上限 **65536**，超出视为协议错误，断开连接。
- `magic` 不对、`version` 不认识、`length` 越界、CRC 错 → 断开连接并记录原因。
- `type` 不认识 → **跳过该帧**，继续解析下一帧。

CRC32 用标准 IEEE 802.3 多项式 `0xEDB88320`（等价于 `zlib.crc32`），初值 `0xFFFFFFFF`，结果取反——即直接用 `zlib.crc32(data)` 的返回值。

---

## 3. 消息类型

| type | 名称 | 方向 | 用途 |
|------|------|------|------|
| 0x01 | `HELLO` | 板 → PC | 连接建立后立即发送，声明版本与板卡信息 |
| 0x02 | `HEARTBEAT` | 双向 | 保活，1 秒一次 |
| 0x10 | `CAN_FRAME` | 双向 | CAN 帧（雷达 ESC ↔ 板卡） |
| 0x20 | `RADAR_UDP` | 双向 | 雷达 UDP 业务数据（40204 / 50007） |
| 0x30 | `STATUS` | 板 → PC | 板卡与链路状态，1 秒一次 |
| 0x40 | `CMD` | PC → 板 | 控制命令 |
| 0x41 | `CMD_ACK` | 板 → PC | 命令应答（成功/失败） |
| 0x50 | `LOG` | 板 → PC | 文本日志，UTF-8 |

---

### 3.1 `HELLO` (0x01)

连接建立后板卡发的第一帧。

```
0   2   proto_ver   uint16，本协议版本（0x0001）
2   1   fw_ver_len
3   …   fw_ver      板卡桥接程序版本字符串
…   1   name_len
…   …   name        板卡名（如 "FZ3B"）
…   1   caps        能力位图 bit0=CAN bit1=RADAR_UDP bit2=CONTROL_50007
```

---

### 3.2 `HEARTBEAT` (0x02)

```
0   4   uptime_ms   uint32，板卡桥接程序启动至今毫秒
```
双向各发各的。**5 秒收不到对端心跳即判定断线**。

---

### 3.3 `CAN_FRAME` (0x10)

承载一条完整的 CAN 帧（8 字节分片后的一条，不是重组后的 DUML 包——重组在上位机做，保留原始信息）。

```
0   4   can_id    uint32；bit31=1 表示扩展帧(EFF)，否则标准帧
4   1   dlc       0~8
5   1   flags     bit0 = RTR（远程帧）
6   8   data      不足 8 字节的剩余部分填 0
```
固定 **14 字节**。

- 板 → PC：来自 `can0` 的接收帧（雷达 ESC → 主机，ID 通常是 `0x0A3`）。
- PC → 板：要发到 `can0` 的帧（主机 → ESC，ID 通常是 `0x0A2`）。

---

### 3.4 `RADAR_UDP` (0x20)

承载一个完整的雷达 UDP 数据报。

```
0   2   port      uint16
2   N   data      原始 UDP 负载（不做任何修改）
```

- 板 → PC：`port` = 数据报的**源端口**（当前只有 40204）。
- PC → 板：`port` = 要发往雷达的**目的端口**（当前 40204 / 50007），板卡会把它发到 `192.168.8.2:port`。

> 板卡当前只**接收** 40204（雷达 → 主机）。50007 是主机 → 雷达的控制通道，需要上位机主动发起时才启用。

---

### 3.5 `STATUS` (0x30)

1 秒一次，固定 **40 字节**。

```
偏移  长度  字段              说明
0     4    uptime_ms         板卡桥接程序运行时间
4     4    can_rx_frames     can0 累计接收帧数
8     4    can_tx_frames     can0 累计发送帧数
12    4    can_err_frames    can0 错误帧累计
16    4    radar_pkts        累计转发的 RADAR_UDP 包数
20    4    radar_bytes       累计转发的 RADAR_UDP 字节数
24    4    dropped           因缓冲满而丢弃的包数
28    1    wifi_connected    1=雷达 STA 已关联，0=未关联
29    1    rssi_dbm          int8，雷达信号强度
30    6    sta_mac           雷达 STA 的 MAC（未连接时全 0）
36    1    can_state         0=ERROR-ACTIVE 1=PASSIVE 2=BUS-OFF
37    1    ap_ready          1=radar_wifi AP 已启动
38    2    status_age        u16，距离上次**成功采集**本帧状态多少秒（0~1 正常）。
                           采集卡死时 STATUS 仍然每秒照发（内容取自缓存），这个数字会一路涨
                           —— 2026-11-11 加，旧桥接这一直填 0
```

> **`status_age` 为什么要存在**：ath6kl 楔死时 `iw`/sysfs 调用永不返回，旧桥接
> 直接把客户端线程钉死在 `build_status()` 里，上位机只收到 HELLO，看不到 STATUS，
> 现象是"连上板卡却一直等待第一帧 0x00F0"。现在采集在后台线程 + 缓存快照，
> STATUS 不会断，`status_age` 负责把"板卡侧已经采集不出新状态了"这件事说出来。
> 板端看门狗用同一个信号（`/tmp/t40_bridge_stat.ts` 的 mtime）决定是否重启板卡。

---

### 3.6 `CMD` (0x40)

```
0   1   op      操作码
1   …   args    参数
```

| op | 名称 | args | 说明 |
|----|------|------|------|
| 0x01 | `SET_RPM` | u16 rpm | 设转速，范围 0~1800 |
| 0x02 | `STOP` | — | 停转（`motor_stop` 0x1E82） |
| 0x03 | `MOTOR_ENABLE` | — | `motor_enable` 0x1E09 |
| 0x04 | `MOTOR_DISABLE` | — | `motor_disable` 0x1E0A |
| 0x05 | `HMS_SWITCH` | u8 on | `hms_switch` 0x1E83 |
| 0x06 | `RAW_CAN` | u32 can_id + u8 dlc + u8[dlc] data | 直接发一条裸 CAN 帧 |
| 0x07 | `SET_STREAM` | u32 mask | bit0=CAN bit1=RADAR_UDP bit2=STATUS，控制板卡往电脑发什么 |
| 0x08 | `PING` | u32 cookie | 板卡原样回 `CMD_ACK`，用于测 RTT |
| 0x09 | `RAW_DUML` | u16 msg_id + payload | 板卡自动组 DUML v1 帧发到 ESC（免去上位机算 CRC） |

> `SET_RPM` / `STOP` / `MOTOR_ENABLE` 等语义命令，板卡内部用 DUML v1 组包（CRC8 init 0x77 / CRC16 init 0x3692），上位机不需要关心 CAN 细节。
> 需要更多控制自由度时用 `RAW_CAN` 或 `RAW_DUML`。

---

### 3.7 `CMD_ACK` (0x41)

```
0   1   op        对应的操作码
1   1   result    0=成功，非 0=错误码
2   4   cookie    仅 PING 用，原样回传
…   …   msg       可选 UTF-8 错误描述
```

---

### 3.8 `LOG` (0x50)

```
0   1   level     0=INFO 1=WARN 2=ERROR
1   N   text      UTF-8 文本
```

---

## 4. 连接与保活

1. 板卡启动后在 `0.0.0.0:46000` 监听。
2. **同一时刻只服务一个客户端**；新连接进来时踢掉旧的。
3. 连接建立后板卡立刻发 `HELLO`，随后每秒发 `HEARTBEAT` 和 `STATUS`。
4. 上位机每秒发 `HEARTBEAT`。任一方 5 秒未收到对方心跳 → 关闭连接。
5. 断开后板卡继续采集 CAN 和雷达数据（只做本地统计），等待新连接。

---

## 5. 数据流与限流

| 流 | 速率 | 说明 |
|----|------|------|
| CAN 帧 | ~70 帧/秒（8 字节/帧） | 雷达 ESC 上报 |
| RADAR_UDP | ~46 包/秒，9 KB/s | 雷达感知数据，突发可到 ~200 包/秒 |
| STATUS | 1 帧/秒 | |
| HEARTBEAT | 1 帧/秒 | |

总带宽 < 100 KB/s，TCP 完全够用。

**板卡侧限流**：发送队列上限 4096 帧，满了丢最旧的并累加 `dropped` 计数（在 `STATUS` 里上报）。CAN 帧**不丢**（速率低且关键）。

---

## 6. 时间戳

- `ts_ns` 统一用板卡 `CLOCK_MONOTONIC`，单位纳秒。
- 上位机首次收到 `HELLO` 时记录本地时间，之后用「本地单调时钟 + 板卡偏移」换算显示时间。
- 不做 NTP 对时——本协议只保证**板卡内部时间轴一致**，跨设备绝对时间不重要。

---

## 7. 版本演进

- `version` 字段只在**不兼容**变更时递增（帧结构改变）。
- 新增 `type` 不算不兼容变更——接收方遇到未知 `type` 必须跳过。
- 新增 `CMD` 的 `op` 同理：未知 `op` 回 `CMD_ACK` 且 `result=1`（不支持）。

---

## 8. 一个最小的收发示例

板卡发一帧 `STATUS`（40 字节 payload，seq=100，ts=1234567890ns）：

```
54 34 30 52                          magic "T40R"
01                                   version 1
30                                   type = STATUS
00 00                                flags
64 00 00 00                          seq = 100
d2 02 96 49 00 00 00 00              ts_ns = 1234567890
28 00 00 00                          length = 40
<40 字节状态>                         payload
<4 字节 crc32>                       CRC32 覆盖 version..payload 末尾
```

上位机发一条 `SET_RPM(900)`：

```
54 34 30 52 | 01 | 40 | 00 00 | <seq> | <ts> | 03 00 00 00 | 01 84 03 | <crc32>
                                                              op=1  rpm=0x0384=900
```
