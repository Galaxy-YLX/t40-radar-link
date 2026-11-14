#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t40_bridge.py —— 板卡侧桥接程序

把 CAN（雷达 ESC）和雷达 UDP 业务数据统一成一种帧格式，通过 TCP 转发给电脑；
电脑发来的命令翻译成 CAN 帧或 UDP 包发给雷达。

协议见 protocol/PROTOCOL.md，默认监听 0.0.0.0:46000。

用法:
    python3 t40_bridge.py                        # 板卡默认（监听 0.0.0.0:46000）
    python3 t40_bridge.py --port 46000 --can can0 --wifi wlx00037f2044d2
    python3 t40_bridge.py --no-can --no-udp      # 只跑 TCP，便于联调上位机
    python3 t40_bridge.py --bind 127.0.0.1 --name LOCAL   # 本机代替板卡

本机模式由 setup/start_local.sh 拉起：不需要 XCZU3EG、不需要第二块网卡
（t40_link AP）和 dnsmasq，本机上位机直接连 127.0.0.1:46000。

注意：板上是 Python 3.6，不要用 3.8+ 语法。
"""
import argparse
import errno
import os
import queue
import select
import socket
import struct
import subprocess
import threading
import time
import zlib

# ------------------------------------------------------------------ 常量
MAGIC = b"T40R"
VERSION = 1
HDR = 24
MAX_PAYLOAD = 65536

T_HELLO = 0x01
T_HEARTBEAT = 0x02
T_CAN = 0x10
T_RADAR_UDP = 0x20
T_STATUS = 0x30
T_CMD = 0x40
T_CMD_ACK = 0x41
T_LOG = 0x50

OP_SET_RPM = 0x01
OP_STOP = 0x02
OP_MOTOR_ENABLE = 0x03
OP_MOTOR_DISABLE = 0x04
OP_HMS = 0x05
OP_RAW_CAN = 0x06
OP_SET_STREAM = 0x07
OP_PING = 0x08
OP_RAW_DUML = 0x09

STREAM_CAN = 0x01
STREAM_UDP = 0x02
STREAM_STATUS = 0x04

CAN_TX_ID = 0x0A2          # 主机 -> ESC
CAN_RX_ID = 0x0A3          # ESC -> 主机
DUML_SENDER = 0x78
DUML_RECEIVER = 0x18

HEARTBEAT_TIMEOUT = 5.0
QUEUE_MAX = 4096

# 状态采集：多久没成功过算卡死、最多换几个线程、心跳文件位置
PROBE_TIMEOUT = 15.0
MAX_LEAK = 5
STAT_FILE = "/tmp/t40_bridge_stat.ts"
WIFI_STICKY = 3.0          # rx 涨过之后多少秒内都算"雷达在线"


# ------------------------------------------------------------------ 工具
def now_ns():
    return time.monotonic_ns() if hasattr(time, "monotonic_ns") else int(time.monotonic() * 1e9)


def crc8(data, init=0x77):
    c = init & 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8C) & 0xFF if (c & 1) else (c >> 1) & 0xFF
    return c


def crc16(data, init=0x3692):
    c = init & 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8408) & 0xFFFF if (c & 1) else (c >> 1) & 0xFFFF
    return c


def build_duml(msg_id, payload=b"", seq=0):
    total = 11 + len(payload) + 2
    f = bytearray(total)
    f[0] = 0x55
    f[1] = total & 0xFF
    f[2] = 0x04 | ((total >> 8) & 0x03)
    f[3] = crc8(f[:3])
    f[4] = DUML_SENDER
    f[5] = DUML_RECEIVER
    struct.pack_into("<H", f, 6, seq & 0xFFFF)
    f[8] = 0x00
    f[9] = (msg_id >> 8) & 0xFF
    f[10] = msg_id & 0xFF
    f[11:11 + len(payload)] = payload
    struct.pack_into("<H", f, total - 2, crc16(f[:-2]))
    return bytes(f)


# ------------------------------------------------------------------ 组帧
def encode_frame(mtype, payload, seq, ts_ns):
    n = len(payload)
    buf = bytearray(HDR + n + 4)
    buf[0:4] = MAGIC
    buf[4] = VERSION
    buf[5] = mtype
    struct.pack_into("<H", buf, 6, 0)
    struct.pack_into("<I", buf, 8, seq & 0xFFFFFFFF)
    struct.pack_into("<Q", buf, 12, ts_ns)
    struct.pack_into("<I", buf, 20, n)
    buf[HDR:HDR + n] = payload
    struct.pack_into("<I", buf, HDR + n, zlib.crc32(bytes(buf[4:HDR + n])) & 0xFFFFFFFF)
    return bytes(buf)


class FrameReader(object):
    """TCP 流 -> 帧。处理半包。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            if len(self.buf) < HDR:
                break
            if bytes(self.buf[0:4]) != MAGIC:
                raise ValueError("bad magic")
            if self.buf[4] != VERSION:
                raise ValueError("bad version %d" % self.buf[4])
            n = struct.unpack_from("<I", self.buf, 20)[0]
            if n > MAX_PAYLOAD:
                raise ValueError("payload too large %d" % n)
            total = HDR + n + 4
            if len(self.buf) < total:
                break
            body = bytes(self.buf[4:HDR + n])
            crc = struct.unpack_from("<I", self.buf, HDR + n)[0]
            if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
                raise ValueError("crc mismatch")
            out.append({
                "type": self.buf[5],
                "seq": struct.unpack_from("<I", self.buf, 8)[0],
                "ts_ns": struct.unpack_from("<Q", self.buf, 12)[0],
                "payload": bytes(self.buf[HDR:HDR + n]),
            })
            del self.buf[0:total]
        return out


# ------------------------------------------------------------------ 桥接主体
class Bridge(object):
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.client = None
        self.client_addr = None
        self.client_rx_ts = 0.0
        self.gen = 0                      # 客户端代号，用来让旧的发送线程自行退出
        self.seq = 0
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.stream_mask = STREAM_CAN | STREAM_UDP | STREAM_STATUS
        self.stat = {
            "can_rx": 0, "can_tx": 0, "can_err": 0,
            "udp_pkts": 0, "udp_bytes": 0, "dropped": 0,
        }
        self.can_sock = None
        self.udp_sock = None
        self.radar_addr = (args.radar_ip, 0)
        # ---- STATUS 采集：常驻线程 + 快照（2026-11-11）----
        # 背景：ath6kl 楔死后 `iw` / 子进程 / sysfs 读会**永不返回**，旧实现里
        # build_status() 在客户端线程里直接采集 → 那个线程被钉死，上位机连上只
        # 收到 HELLO，STATUS/HEARTBEAT 一帧没有，看起来像"雷达没出点"。
        # 现在：采集在独立线程里跑，客户端只读快照；采集线程卡死了就由 supervisor
        # 换一个新的（最多换 MAX_LEAK 次）。STATUS 永远照发，最后 2 字节是
        # "距离上次成功采集多少秒"，上位机和 hw_wdt 据此判死。
        self._st_lock = threading.Lock()
        self._st_cache = None      # 最近一次成功的 STATUS 负载
        self._st_ok_ts = 0.0       # 最近一次成功采集的时刻（0 = 一次都没成功）
        self._st_gen = 0           # 采集线程代号：新线程接班后旧的自行退出
        self._st_spawn_ts = 0.0    # 当前采集线程是什么时候拉起的
        self._st_leaked = 0        # 卡死被放弃的采集线程数
        self._st_cap_logged = False

    # -------------------------------------------------- 发送队列
    def push(self, mtype, payload):
        if mtype == T_CAN and not (self.stream_mask & STREAM_CAN):
            return
        if mtype == T_RADAR_UDP and not (self.stream_mask & STREAM_UDP):
            return
        if mtype == T_STATUS and not (self.stream_mask & STREAM_STATUS):
            return
        # 没有客户端时不排队，否则会积压，上位机一连上就被历史帧冲垮
        if self.client is None:
            return
        q = self.q
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            seq = self.seq
        frame = encode_frame(mtype, payload, seq, now_ns())
        try:
            q.put_nowait(frame)
        except queue.Full:
            # 丢最旧的，保证新数据能进去
            try:
                q.get_nowait()
                q.put_nowait(frame)
            except Exception:
                pass
            self.stat["dropped"] += 1

    def log(self, text, level=0):
        # 同时写 stdout（/root/bridge.log）—— 没有客户端时 push 会直接丢掉
        try:
            print("[%s] %s%s" % (time.strftime("%H:%M:%S"),
                                 "[错误] " if level == 2 else "[警告] " if level == 1 else "",
                                 text), flush=True)
        except Exception:
            pass
        self.push(T_LOG, bytes([level]) + text.encode("utf-8", "replace"))

    # -------------------------------------------------- CAN
    def can_reader(self):
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            s.bind((self.args.can,))
        except Exception as exc:
            self.log("CAN 打开失败: %s" % exc, 2)
            return
        self.can_sock = s
        s.settimeout(0.5)
        self.log("CAN 就绪: %s" % self.args.can)
        while True:
            try:
                cf, _ = s.recvfrom(16)
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.2)
                continue
            can_id, dlc = struct.unpack("<IB", cf[:5])
            data = cf[8:8 + min(dlc, 8)]
            self.stat["can_rx"] += 1
            payload = struct.pack("<IBB8s", can_id & 0x1FFFFFFF, dlc, 0, data.ljust(8, b"\0"))
            self.push(T_CAN, payload)

    def can_send_raw(self, can_id, data):
        if self.can_sock is None:
            return False
        try:
            self.can_sock.send(struct.pack("<IB3x8s", can_id, len(data), data.ljust(8, b"\0")))
            self.stat["can_tx"] += 1
            return True
        except Exception:
            return False

    def can_send_duml(self, msg_id, payload=b""):
        frame = build_duml(msg_id, payload, seq=int(time.time()) & 0xFFFF)
        ok = True
        for off in range(0, len(frame), 8):
            ok = self.can_send_raw(CAN_TX_ID, frame[off:off + 8]) and ok
        return ok

    # -------------------------------------------------- 雷达 UDP
    def udp_reader(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 雷达突发大包（0x00F0 分片能到 ~5000 点）时默认 208KB 缓冲会丢，
            # 尽力调大；内核会把值翻倍并截到 net.core.rmem_max。
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except Exception:
                pass
            s.bind(("0.0.0.0", self.args.udp_port))
        except Exception as exc:
            self.log("UDP %d 绑定失败: %s" % (self.args.udp_port, exc), 2)
            return
        self.udp_sock = s
        s.settimeout(0.5)
        self.log("雷达 UDP 就绪: :%d" % self.args.udp_port)
        while True:
            try:
                # 0x00F0 单包可达几 KB~几十 KB（5000 点 × 12B），4096 会把
                # 帧尾截掉，上位机只能解出一个残缺的点云。UDP 最大负载 65507。
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.2)
                continue
            self.stat["udp_pkts"] += 1
            self.stat["udp_bytes"] += len(data)
            self.push(T_RADAR_UDP, struct.pack("<H", self.args.udp_port) + data)

    def udp_send(self, port, data):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(data, (self.args.radar_ip, port))
            s.close()
            return True
        except Exception:
            return False

    # -------------------------------------------------- 状态采集
    def read_wifi(self):
        """判断雷达是否关联：只看 /proc/net/dev 的 rx 增量，**不碰 iw / sysfs**。

        AP 模式下 /proc/net/wireless 拿不到链路质量，所以以网卡 rx_packets 的增量
        作为判据：有包进来就说明 STA 在通信。

        2026-11-11：原来这里每 5 s 跑一次 `iw dev <if> station dump` 取 rssi，而
        ath6kl 楔死时这个子进程会进 D 状态、`timeout` 杀不掉、`communicate()` 无限等
        —— 整个 STATUS 采集就永久挂住，上位机只看到 CAN。RSSI 只是好看用的，
        直接删掉；rx 计数改读 /proc/net/dev（netdev 核心维护，设备卡死也能读）。
        """
        rssi = 0
        rx = 0
        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    if line.split(":")[0].strip() == self.args.wifi:
                        rx = int(line.split(":")[1].split()[1])
                        break
        except Exception:
            rx = 0
        prev = getattr(self, "_last_rx", None)
        self._last_rx = rx
        now = time.time()
        # 2 Hz 采样时很容易正好落在雷达的发送间隙里，用"最近 3 秒内涨过"更稳，
        # 否则上位机的 wifi_connected 会 0/1 抖。
        if prev is None:
            if rx > 0:
                self._rx_ts = now
        elif rx > prev:
            self._rx_ts = now
        connected = 1 if (now - getattr(self, "_rx_ts", 0.0)) < WIFI_STICKY else 0
        return connected, rssi, b"\0" * 6

    def read_can_state(self):
        """看 CAN 是不是 BUS-OFF / ERROR-PASSIVE。

        用 Popen + 轮询，超时只 kill **不 wait**：子进程万一进 D 状态，
        `check_output(timeout=)` 里的 communicate() 会永远等下去，采集线程就挂了。
        """
        try:
            p = subprocess.Popen(["ip", "-d", "link", "show", self.args.can],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            return 0
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if p.poll() is not None:
                try:
                    out = p.stdout.read().decode("utf-8", "replace")
                except Exception:
                    return 0
                if "BUS-OFF" in out:
                    return 2
                if "ERROR-PASSIVE" in out:
                    return 1
                return 0
            time.sleep(0.05)
        try:
            p.kill()
        except Exception:
            pass
        # 顺手收尸：wait 带超时走 WNOHANG 轮询，子进程真在 D 状态也不会把这里挂住。
        # 不收的话每卡一次就留一个僵尸，跑几天就堆满了。
        try:
            p.wait(timeout=0.2)
        except Exception:
            pass
        try:
            p.stdout.close()
        except Exception:
            pass
        return 0

    def build_status(self):
        """返回 STATUS 负载。**永不阻塞、永不启动线程** —— 只读采集线程留下的快照。

        负载最后 2 字节（原先是保留字段）= 距离上次成功采集的秒数。
        正常时 0~1；采集线程卡死就一路涨，上位机和 hw_wdt 据此判死。
        """
        with self._st_lock:
            cache = self._st_cache
        if cache is not None:
            return self._status_touch(cache)
        # 一次都没成功过：给一份空状态，至少让上位机看到"板卡活着但状态未知"
        return self._status_pack(0, 0, 0, 0, 0, 0, 0, 0, b"\0" * 6, 0, 0)

    def status_worker(self, gen):
        """状态采集执行体。**这个线程允许卡死**：卡住只会让快照变旧。

        喂给 STATUS 的字段每轮刷新一次；`_st_ok_ts` 是"采集还活着"的唯一凭据，
        supervisor 看它不涨就换一个新线程。被接班后旧线程自行退出（gen 不匹配）。
        """
        period = 1.0 / max(0.2, float(self.args.status_hz))
        while True:
            st = None
            try:
                st = self._status_now()
            except Exception as exc:
                self.log("状态采集异常: %s" % exc, 1)
            now = time.time()
            with self._st_lock:
                if gen != self._st_gen:
                    return                    # 已经被新线程接班
                if st is not None:
                    self._st_cache = st
                    self._st_ok_ts = now
                    self._st_leaked = 0       # 采集恢复正常，放弃计数清零
            if st is not None:
                self._write_stat_ts(now, st)
            time.sleep(period)

    def status_supervisor(self):
        """盯着采集线程：超过 PROBE_TIMEOUT 没成功过就换一个。

        为什么不是"卡住就不管"：一次卡死就永久失去 STATUS，等驱动自己缓过来也没用。
        也不能无限换：真楔死时每 15 s 起一个线程，最后会堆一堆 D 状态线程，
        所以最多换 MAX_LEAK 次，之后只让 STATUS 里的陈旧秒数继续涨。

        判据取 max(上次成功, 上次拉起新线程)：否则"卡住"这个条件会一直成立，
        一秒一个把额度瞬间打光，新线程根本没机会跑。
        """
        while True:
            time.sleep(1.0)
            now = time.time()
            with self._st_lock:
                ok = self._st_ok_ts
                last = max(ok, self._st_spawn_ts)
            if last and (now - last) < PROBE_TIMEOUT:
                continue
            with self._st_lock:
                if self._st_leaked >= MAX_LEAK:
                    if not self._st_cap_logged:
                        self._st_cap_logged = True
                        self.log("状态采集连续 %d 次卡死，不再换线程（STATUS 里会显示"
                                 "陈旧秒数），等硬件看门狗复位" % MAX_LEAK, 2)
                    continue
                self._st_leaked += 1
                n = self._st_leaked
                self._st_gen += 1
                gen = self._st_gen
                self._st_spawn_ts = now
            if not ok and n == 1:
                self.log("状态采集线程启动")
            elif not ok:
                self.log("状态采集从未成功，换新线程（第 %d/%d 次）" % (n, MAX_LEAK), 1)
            else:
                self.log("状态采集已 %.0f s 没成功，换新线程（第 %d/%d 次）"
                         % (now - ok, n, MAX_LEAK), 1)
            t = threading.Thread(target=self.status_worker, args=(gen,),
                                 name="t40-status-%d" % gen)
            t.daemon = True
            t.start()

    def _write_stat_ts(self, now, st):
        """心跳文件：板端 radar_watchdog.sh 和 hw_wdt.py 都靠它的 mtime 判断采集死没死。

        注意它现在由采集线程独立维护，**和有没有上位机连着无关** —— 否则没人看的时候
        文件不更新，看门狗会把一块健康的板子重启掉。

        内容里带 wifi=/udp= 只是给看门狗日志用（省得它再去 iw 里问一次）。
        """
        try:
            f = open(STAT_FILE, "w")
            try:
                f.write("%d can_rx=%d udp_pkts=%d wifi=%d\n"
                        % (int(now), self.stat["can_rx"], self.stat["udp_pkts"],
                           st[28] if len(st) > 28 else 0))
            finally:
                f.close()
        except Exception:
            pass

    def _status_now(self):
        """真正去读网卡/CAN（可能卡住，所以只在采集线程里调用）。"""
        connected, rssi, mac = self.read_wifi()
        try:
            ap_ready = 1 if os.path.exists("/sys/class/net/%s" % self.args.wifi) else 0
        except Exception:
            ap_ready = 0
        return self._status_pack(
            self.stat["can_rx"], self.stat["can_tx"], self.stat["can_err"],
            self.stat["udp_pkts"], self.stat["udp_bytes"], self.stat["dropped"],
            connected, rssi, mac, self.read_can_state(), ap_ready)

    def _status_pack(self, can_rx, can_tx, can_err, udp_pkts, udp_bytes, dropped,
                     connected, rssi, mac, can_state, ap_ready):
        return struct.pack(
            "<IIIIIIIBb6sBBH",
            int((time.time() - self.t0) * 1000) & 0xFFFFFFFF,
            can_rx & 0xFFFFFFFF,
            can_tx & 0xFFFFFFFF,
            can_err & 0xFFFFFFFF,
            udp_pkts & 0xFFFFFFFF,
            udp_bytes & 0xFFFFFFFF,
            dropped & 0xFFFFFFFF,
            connected, rssi, mac,
            can_state, ap_ready, self._status_age(),
        )

    def _status_age(self):
        """距离上次成功采集多少秒（0xFFFF 封顶）。

        从没成功过就用"桥接启动至今"—— 这样刚起来是 0、1、2…，不会一上来就报
        65535 让上位机误以为卡了很久。
        """
        base = self._st_ok_ts if self._st_ok_ts > 0 else self.t0
        age = int(time.time() - base)
        if age < 0:
            age = 0
        elif age > 0xFFFF:
            age = 0xFFFF
        return age

    def _status_touch(self, pkt):
        """缓存包只刷新"新鲜度"这 2 字节，其余字段保持上次成功采集的值。"""
        return pkt[:38] + struct.pack("<H", self._status_age())

    # -------------------------------------------------- 命令处理
    def handle_cmd(self, payload):
        if not payload:
            return
        op = payload[0]
        args = payload[1:]
        result = 0
        msg = ""
        cookie = 0
        try:
            if op == OP_SET_RPM:
                rpm = struct.unpack("<H", args[:2])[0]
                if not self.can_send_duml(0x1E80, struct.pack("<H", rpm)):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_STOP:
                if not self.can_send_duml(0x1E82):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_MOTOR_ENABLE:
                if not self.can_send_duml(0x1E09):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_MOTOR_DISABLE:
                if not self.can_send_duml(0x1E0A):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_HMS:
                if not self.can_send_duml(0x1E83, bytes([args[0] if args else 0])):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_RAW_CAN:
                can_id = struct.unpack("<I", args[:4])[0]
                dlc = args[4]
                data = args[5:5 + dlc]
                if not self.can_send_raw(can_id, data):
                    result, msg = 3, "CAN 发送失败"
            elif op == OP_SET_STREAM:
                self.stream_mask = struct.unpack("<I", args[:4])[0]
            elif op == OP_PING:
                cookie = struct.unpack("<I", args[:4])[0] if len(args) >= 4 else 0
            elif op == OP_RAW_DUML:
                msg_id = struct.unpack("<H", args[:2])[0]
                if not self.can_send_duml(msg_id, args[2:]):
                    result, msg = 3, "CAN 发送失败"
            else:
                result, msg = 1, "不支持的 op 0x%02X" % op
        except Exception as exc:
            result, msg = 2, "参数错误: %s" % exc

        ack = bytes([op, result]) + struct.pack("<I", cookie) + msg.encode("utf-8", "replace")
        self.push(T_CMD_ACK, ack)

    # -------------------------------------------------- 客户端
    def sender_loop(self, conn, q, gen):
        """独占 socket 的发送线程。

        必须和收命令的循环分开：以前发送排在 conn.recv(timeout=0.5) 之后，
        上位机只发 1Hz 心跳时，雷达包要在队列里干等最多 500ms
        （实测 p50=251ms / p99=495ms）。现在数据一到就发。
        """
        while True:
            try:
                first = q.get(timeout=0.2)
            except queue.Empty:
                if gen != self.gen:
                    return
                continue
            if gen != self.gen:
                return
            # 把队列里已有的帧一次打包发出去，减少小包和系统调用
            batch = [first]
            try:
                while len(batch) < 64:
                    batch.append(q.get_nowait())
            except queue.Empty:
                pass
            try:
                conn.sendall(b"".join(batch))
            except Exception:
                return

    def serve_client(self, conn, addr):
        self.log("上位机接入 %s:%d" % addr)
        reader = FrameReader()
        conn.settimeout(None)
        self.client_rx_ts = time.time()
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.client = conn
            # 每个连接一条独立队列：旧连接的发送线程拿的是旧队列对象，
            # 不会和新连接抢帧，也不用担心残留帧冲垮新连接
            self.q = queue.Queue(maxsize=QUEUE_MAX)
        q = self.q
        # HELLO（此时发送线程还没起，独占 socket）
        hello = struct.pack("<H", 1) + bytes([len("1.0.0")]) + b"1.0.0"
        name = self.args.name.encode("utf-8", "replace")[:255]
        hello += bytes([len(name)]) + name + bytes([0x07])
        try:
            conn.sendall(encode_frame(T_HELLO, hello, 0, now_ns()))
        except Exception:
            self._drop_client(conn, addr)
            return

        threading.Thread(target=self.sender_loop, args=(conn, q, gen),
                         daemon=True).start()

        last_st = 0.0
        while gen == self.gen:
            try:
                r, _, _ = select.select([conn], [], [], 0.2)
            except Exception:
                break
            if r:
                try:
                    data = conn.recv(65536)
                except Exception:
                    break
                if not data:
                    break
                self.client_rx_ts = time.time()
                try:
                    for fr in reader.feed(data):
                        if fr["type"] == T_CMD:
                            self.handle_cmd(fr["payload"])
                        elif fr["type"] == T_HEARTBEAT:
                            pass
                except Exception as exc:
                    self.log("帧解析失败，断开: %s" % exc, 1)
                    break
            # 心跳超时
            if time.time() - self.client_rx_ts > HEARTBEAT_TIMEOUT:
                self.log("上位机心跳超时，断开", 1)
                break
            # 心跳 + 状态也走队列，避免两个线程同时往 socket 写
            t = time.time()
            if t - last_st >= 1.0:
                last_st = t
                self.push(T_STATUS, self.build_status())
                self.push(T_HEARTBEAT,
                          struct.pack("<I", int((t - self.t0) * 1000) & 0xFFFFFFFF))
        self._drop_client(conn, addr)

    def _drop_client(self, conn, addr):
        try:
            conn.close()
        except Exception:
            pass
        with self.lock:
            if self.client is conn:
                self.client = None
        self.log("上位机断开 %s:%d" % addr)

    def tcp_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.bind, self.args.port))
        srv.listen(1)
        self.log("TCP 监听 %s:%d" % (self.args.bind, self.args.port))
        print("listening on %s:%d" % (self.args.bind, self.args.port), flush=True)
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                old = self.client
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            threading.Thread(target=self.serve_client, args=(conn, addr), daemon=True).start()

    # -------------------------------------------------- 启动
    def run(self):
        if not self.args.no_can:
            threading.Thread(target=self.can_reader, daemon=True).start()
        if not self.args.no_udp:
            threading.Thread(target=self.udp_reader, daemon=True).start()
        # 采集与客户端无关：没人连也要每秒刷新，否则心跳文件不动、
        # radar_watchdog / hw_wdt 会把一块健康的板子判死。
        threading.Thread(target=self.status_supervisor, daemon=True,
                         name="t40-status-sup").start()
        self.tcp_server()


def main():
    ap = argparse.ArgumentParser(description="T40 雷达数据桥接（板卡 / 本机）")
    ap.add_argument("--port", type=int, default=46000, help="TCP 监听端口")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="TCP 监听地址（本机代替板卡时用 127.0.0.1）")
    ap.add_argument("--name", default="FZ3B", help="HELLO 里的节点名")
    ap.add_argument("--can", default="can0")
    ap.add_argument("--wifi", default="wlx00037f2044d2", help="雷达 AP 网卡名")
    ap.add_argument("--udp-port", type=int, default=40204, help="雷达业务 UDP 端口")
    ap.add_argument("--radar-ip", default="192.168.8.2")
    ap.add_argument("--no-can", action="store_true")
    ap.add_argument("--no-udp", action="store_true")
    ap.add_argument("--status-hz", type=float, default=2.0,
                    help="状态采集频率（默认 2 Hz）")
    a = ap.parse_args()
    Bridge(a).run()


if __name__ == "__main__":
    main()
