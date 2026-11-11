#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t40_cloud_view.py —— T40 雷达 0x00F0 点云实时显示（带自愈与错误可见）

一条命令就能跑：

    # 默认就是这个：走板卡转发（电脑连 t40_link，板卡 192.168.9.1）
    # 连不上时还会自动让 Windows 去连 t40_link 这个 WiFi（--no-wifi 可关）
    python t40_cloud_view.py

    # 2) 直连雷达 UDP（电脑在雷达 AP 网段时）
    python t40_cloud_view.py --src udp://0.0.0.0:40204

    # 3) 回放录好的 pcap（自己用 tcpdump 抓的 40204 流量）
    python t40_cloud_view.py --src pcap:path/to/capture.pcap --speed 2

    # 4) 没有硬件时看界面
    python t40_cloud_view.py --src demo

    # 5) 不开界面，自检解码链路
    python t40_cloud_view.py --selftest path/to/capture.pcap

依赖：pip install PySide6 pyqtgraph numpy

稳定性设计（2026-11-10 加）：
  * TCP 源**断线自动重连**（1→5 s 退避），所以"板卡比电脑晚起来"也能自己接上；
  * 界面顶部红/黄/绿状态条 + 板卡 STATUS（wifi_connected / radar_pkts / dropped）；
  * 看门狗：连上了却 >3 s 没有 0x00F0 → 直接告诉你"雷达没在发"还是"雷达 WiFi 没连"；
  * 所有异常写 t40_cloud_view.log；pythonw 下没有控制台，致命错误用弹窗兜底；
  * 转速/马达控制：CMD 0x40 → 板卡转成 CAN 0x1E80/0x1E09/0x1E0A 发给雷达 ESC。

坐标系（2026-11-10 现场锚定）：c0 = 0 是雷达正前方。画面朝向可调，**默认整幅画面
逆时针转 180° 且方位逆时针**（面板「旋转」/「方位逆时针」，或 --rot / --cw）；
把「旋转」调回 0、勾掉逆时针，就是老样子：0° 朝上、顺时针。
"""
from __future__ import annotations

import argparse
import collections
import locale
import os
import struct
import subprocess
import sys
import threading
import time
import zlib

import numpy as np

# ----------------------------------------------------------------- 协议常量
T40R_MAGIC = b"T40R"
T40R_HDR = 24
T_HELLO, T_HEARTBEAT, T_RADAR_UDP, T_STATUS = 0x01, 0x02, 0x20, 0x30
T_CMD, T_CMD_ACK, T_LOG = 0x40, 0x41, 0x50
OP_SET_RPM, OP_STOP, OP_MOTOR_ENABLE, OP_MOTOR_DISABLE = 0x01, 0x02, 0x03, 0x04

DUML_MAGIC = b"\x03\x10\x07"
HDR19, REC12 = 19, 12
TWO_PI = 6.28319
AZ_STEP = 0.0234438          # rad / 方位格（268 格一圈）
BLIND = (5.204, 7.314)       # 无数据扇区，正中 ≈ 0°（正前方）

# 画面朝向默认值（2026-11-11 现场要求）：整幅画面逆时针转 180°（等于倒过来），
# 方位默认逆时针。面板里能随时改回 0°/顺时针。
ROT_DEFAULT = 180            # 度，逆时针
CCW_DEFAULT = True           # 方位逆时针

LOG_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "t40_cloud_view.log"))


# ================================================================= 日志 / 兜底
def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def fatal(msg, title="T40 雷达点云"):
    """致命错误：写日志 + 弹窗（pythonw 没有控制台，不弹窗就等于静默死掉）。"""
    log("FATAL: " + msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(msg)[:1800], title, 0x10)
    except Exception:
        print(msg)


# ================================================================= WiFi（Windows）
def _decode(raw):
    """netsh 在中文 Windows 上输出 GBK；用 text=True 会直接抛 UnicodeDecodeError。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in (locale.getpreferredencoding(False), "gbk", "utf-8"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def wifi_state():
    """当前连着的 SSID；取不到返回 ''。"""
    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                           capture_output=True, timeout=6,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = _decode(r.stdout)
    except Exception:
        return ""
    for line in out.splitlines():
        t = line.strip()
        if t.upper().startswith("BSSID"):
            continue
        if t.upper().startswith("SSID") and ":" in t:
            return t.split(":", 1)[1].strip()
    return ""


def wifi_connect(ssid):
    """让 Windows 连到指定 SSID（profile 要已存在）。返回 (ok, 说明)。"""
    if sys.platform != "win32" or not ssid:
        return False, "非 Windows 或未指定 SSID"
    try:
        r = subprocess.run(["netsh", "wlan", "connect", "name=%s" % ssid, "ssid=%s" % ssid],
                           capture_output=True, timeout=12,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        msg = (_decode(r.stdout) or _decode(r.stderr)).strip().replace("\r\n", " ")
        return r.returncode == 0, msg[:160]
    except Exception as exc:
        return False, str(exc)


# ================================================================= 解码
def decode_00f0(payload):
    """单个 UDP 负载 → 点云数组；不是 0x00F0 就返回 None。"""
    i = payload.find(DUML_MAGIC)
    if i < 0:
        return None
    body = payload[i:]
    if len(body) < HDR19:
        return None
    nbytes = struct.unpack_from("<H", body, 13)[0]
    if nbytes == 0 or nbytes % REC12 or HDR19 + nbytes > len(body):
        return None
    raw = body[HDR19:HDR19 + nbytes]
    rec = np.frombuffer(raw, "<u2").reshape(-1, 6)
    ok = (body[17] == (sum(raw) & 0xFF)) and (body[18] == (sum(body[1:18]) & 0xFF))
    return dict(az=rec[:, 0].astype(np.float64) / 1000.0,
                el=rec[:, 1].astype(np.float64) / 1000.0 - TWO_PI,
                r=rec[:, 2].astype(np.float64) / 1000.0,
                c3=rec[:, 3].astype(np.float32),
                c4=rec[:, 4].astype(np.float32),
                c5=rec[:, 5].astype(np.float64),
                seq=struct.unpack_from("<H", body, 3)[0],
                n=len(rec), sum_ok=bool(ok))


def points_xy(az, el, r, clockwise=True, rot=0.0):
    """球坐标 → 俯视平面坐标。0° 朝上（+y），clockwise=True 时方位顺时针增加。

    rot 是**画面逆时针旋转角**（弧度），默认 180°（见 ROT_DEFAULT）：
    它转的是整幅画面（点云、盲区、前/后标注一起转），不改雷达的方位数据本身。
    0° 落在上方只是 rot=0 时的样子；rot=180 时雷达 0°（正前方）会跑到下方。
    """
    d = r * np.cos(el)
    s = az if clockwise else -az
    if rot:
        s = s - rot
    return d * np.sin(s), d * np.cos(s), r * np.sin(el)


def rpm_from_c5(az, c5):
    """同一帧里 c5 对方位的斜率 ⇒ 转速（µs 与 mrad 都是固件原生量，免标定）。"""
    if len(az) < 12:
        return None
    pair = {}
    for a, t in zip(az, c5):
        pair.setdefault(int(a * 1000), t)
    pts = sorted(pair.items())
    if len(pts) < 8:
        return None
    ys, base, prev = [], 0.0, pts[0][1]
    for _, t in pts:
        d = (t - prev) % 65536.0
        if d > 32768:
            d -= 65536
        base += d
        prev = t
        ys.append(base)
    n = len(pts)
    mx = sum(a for a, _ in pts) / n
    my = sum(ys) / n
    num = sum((a - mx) * (y - my) for (a, _), y in zip(pts, ys))
    den = sum((a - mx) ** 2 for a, _ in pts)
    if den <= 0 or num <= 0:
        return None
    return 60e6 / (num / den * 6283.19)


def _now_ns():
    return time.monotonic_ns()


# ================================================================= 数据源
class Source(object):
    """数据源基类：run() 在子线程里跑，emit(ts, payload, who) 交数据。"""
    name = "?"
    note = None
    can_cmd = False
    on_status = None          # 板卡 STATUS → dict
    on_ack = None             # 命令应答 → (op, result, msg)

    def __init__(self, emit, take_cmds=None):
        self._emit = emit
        self._take_cmds = take_cmds or (lambda: [])
        self.running = False

    def _status(self, text):
        if self.note:
            self.note(text)

    def stop(self):
        self.running = False


class UdpSource(Source):
    """直接监听雷达 40204 的 UDP。"""
    def __init__(self, bind, port, emit, take_cmds=None):
        super().__init__(emit, take_cmds)
        self.bind, self.port = bind, port
        self.name = "udp %s:%d" % (bind, port)

    def run(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bind, self.port))
        except Exception as exc:
            self._status("绑定 %s:%d 失败：%s" % (self.bind, self.port, exc))
            return
        s.settimeout(0.3)
        self._status("监听 UDP %s:%d" % (self.bind, self.port))
        while self.running:
            try:
                data, addr = s.recvfrom(65535)
            except Exception:
                continue
            self._emit(_now_ns(), data, addr[0])
        s.close()


class TcpSource(Source):
    """走板卡 t40_bridge.py 的 TCP 46000：断线自动重连 + 命令通道。"""
    can_cmd = True

    def __init__(self, host, port, emit, take_cmds=None, wifi=None):
        super().__init__(emit, take_cmds)
        self.host, self.port = host, port
        self.wifi = wifi                 # 连不上时尝试加入的 SSID（None = 不管）
        self._last_wifi_try = 0.0
        self.name = "tcp %s:%d" % (host, port)

    @staticmethod
    def _frame(mtype, payload=b""):
        n = len(payload)
        buf = bytearray(T40R_HDR + n + 4)
        buf[0:4] = T40R_MAGIC
        buf[4] = 0x01
        buf[5] = mtype
        struct.pack_into("<Q", buf, 12, _now_ns())
        struct.pack_into("<I", buf, 20, n)
        buf[T40R_HDR:T40R_HDR + n] = payload
        struct.pack_into("<I", buf, T40R_HDR + n,
                         zlib.crc32(bytes(buf[4:T40R_HDR + n])) & 0xFFFFFFFF)
        return bytes(buf)

    def run(self):
        import socket
        backoff = 1.0
        while self.running:
            sock = None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(0.2)
                self._status("已连上 %s:%d" % (self.host, self.port))
                log("TCP 已连上 %s:%d" % (self.host, self.port))
                backoff = 1.0
                self._serve(sock)
                if self.running:
                    self._status("连接断开，准备重连 …")
                    log("TCP 断开")
            except Exception as exc:
                self._status("连接 %s:%d 失败：%s（%.0f s 后重试）"
                             % (self.host, self.port, exc, backoff))
                log("TCP 连接失败：%s" % exc)
                self._try_wifi()
            finally:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass
            if not self.running:
                break
            t_end = time.time() + backoff
            while self.running and time.time() < t_end:
                time.sleep(0.1)
            backoff = min(backoff * 1.7, 5.0)

    def _try_wifi(self):
        """连不上板卡时：如果不是连着目标 SSID，就让 Windows 去连（最多 15 s 一次）。"""
        if not self.wifi or sys.platform != "win32":
            return
        now = time.time()
        if now - self._last_wifi_try < 15.0:
            return
        self._last_wifi_try = now
        cur = wifi_state()
        if cur == self.wifi:
            return                       # 已经连着，问题不在 WiFi
        self._status("尝试连接 WiFi %s（当前：%s）…" % (self.wifi, cur or "未连接"))
        ok, msg = wifi_connect(self.wifi)
        log("WiFi %s -> %s %s" % (self.wifi, "OK" if ok else "FAIL", msg))
        if ok:
            self._status("已请求连接 WiFi %s，等板卡上线" % self.wifi)

    def _serve(self, sock):
        import socket
        buf = bytearray()
        last_hb = 0.0
        while self.running:
            t = time.time()
            # 命令（转速/马达）在这里发，保证只有本线程碰 socket
            for payload in self._take_cmds():
                try:
                    sock.sendall(self._frame(T_CMD, payload))
                    self._status("已下发命令 op=0x%02X" % payload[0])
                except Exception as exc:
                    self._status("命令发送失败：%s" % exc)
                    return
            if t - last_hb >= 1.0:
                last_hb = t
                try:
                    sock.sendall(self._frame(T_HEARTBEAT,
                                             struct.pack("<I", int(t) & 0xFFFFFFFF)))
                except Exception:
                    return
            try:
                data = sock.recv(262144)
            except socket.timeout:
                continue
            except Exception:
                return
            if not data:
                return
            buf += data
            while len(buf) >= T40R_HDR:
                if bytes(buf[0:4]) != T40R_MAGIC:
                    i = buf.find(T40R_MAGIC, 1)
                    if i < 0:
                        buf.clear()
                        break
                    del buf[:i]
                    continue
                n = struct.unpack_from("<I", buf, 20)[0]
                if n > 65536:
                    buf.clear()
                    break
                if len(buf) < T40R_HDR + n + 4:
                    break
                body = bytes(buf[4:T40R_HDR + n])
                if (zlib.crc32(body) & 0xFFFFFFFF) == struct.unpack_from("<I", buf, T40R_HDR + n)[0]:
                    self._dispatch(buf[5], bytes(buf[T40R_HDR:T40R_HDR + n]),
                                   struct.unpack_from("<Q", buf, 12)[0], n)
                del buf[:T40R_HDR + n + 4]

    def _dispatch(self, mtype, payload, ts, n):
        if mtype == T_RADAR_UDP and n > 2:
            port = struct.unpack_from("<H", payload, 0)[0]
            if port == 40204:
                self._emit(ts, bytes(payload[2:]), "radar")
        elif mtype == T_STATUS and n >= 34:
            st = dict(uptime_ms=struct.unpack_from("<I", payload, 0)[0],
                      can_rx=struct.unpack_from("<I", payload, 4)[0],
                      can_tx=struct.unpack_from("<I", payload, 8)[0],
                      can_err=struct.unpack_from("<I", payload, 12)[0],
                      radar_pkts=struct.unpack_from("<I", payload, 16)[0],
                      radar_bytes=struct.unpack_from("<I", payload, 20)[0],
                      dropped=struct.unpack_from("<I", payload, 24)[0],
                      wifi=payload[28],
                      rssi=struct.unpack_from("<b", payload, 29)[0],
                      can_state=payload[36] if n >= 40 else 0,
                      # 板卡侧"距离上次成功采集多少秒"（新桥接才有；旧桥接是保留字段 0）
                      status_age=struct.unpack_from("<H", payload, 38)[0] if n >= 40 else 0)
            if self.on_status:
                self.on_status(st)
        elif mtype == T_CMD_ACK and n >= 6:
            op, res = payload[0], payload[1]
            msg = payload[6:].decode("utf-8", "replace") if n > 6 else ""
            if self.on_ack:
                self.on_ack((op, res, msg))
        elif mtype == T_LOG and n:
            self._status("板卡日志：" + payload.decode("utf-8", "replace").strip()[:200])


class PcapSource(Source):
    """回放 pcap（自带最小 pcap 解析 + IPv4 分片重组）。"""
    def __init__(self, path, emit, speed=1.0, loop=True, take_cmds=None):
        super().__init__(emit, take_cmds)
        self.path, self.speed, self.loop = path, max(speed, 0.01), loop
        self.name = "pcap %s" % os.path.basename(path)

    @staticmethod
    def _read(path):
        d = open(path, "rb").read()
        magic = struct.unpack_from("<I", d, 0)[0]
        if magic in (0xa1b2c3d4, 0xa1b23c4d):
            endian, nano = "<", magic == 0xa1b23c4d
        elif magic in (0xd4c3b2a1, 0x4d3cb2a1):
            endian, nano = ">", magic == 0x4d3cb2a1
        else:
            raise ValueError("不是 pcap：%08x" % magic)
        link = struct.unpack_from(endian + "I", d, 20)[0]
        if link != 1:
            raise ValueError("只支持 Ethernet pcap（linktype=%d）" % link)
        off, n, out = 24, len(d), []
        while off + 16 <= n:
            ts_s, ts_u, incl, _ = struct.unpack_from(endian + "IIII", d, off)
            off += 16
            out.append((ts_s + ts_u * (1e-9 if nano else 1e-6), d[off:off + incl]))
            off += incl
        return out

    @staticmethod
    def _udp_40204(frames):
        bufs, done = {}, []
        for ts, pkt in frames:
            if len(pkt) < 14 or pkt[12:14] != b"\x08\x00":
                continue
            ip = pkt[14:]
            if len(ip) < 20 or (ip[0] >> 4) != 4 or ip[9] != 17:
                continue
            ihl = (ip[0] & 0xF) * 4
            tot = struct.unpack_from(">H", ip, 2)[0]
            ident, ff = struct.unpack_from(">HH", ip, 4)
            body = ip[ihl:tot]
            frag_off = (ff & 0x1FFF) * 8
            mf = bool(ff & 0x2000)
            if not mf and frag_off == 0:
                done.append((ts, body))
                continue
            key = (ip[12:16], ip[16:20], ident)
            bufs.setdefault(key, {})[frag_off] = body
            if not mf:
                frs = bufs.pop(key, None)
                if not frs:
                    continue
                b = bytearray()
                for o in sorted(frs):
                    if o != len(b):
                        b = None
                        break
                    b += frs[o]
                if b:
                    done.append((ts, bytes(b)))
        out = []
        for ts, ipbody in done:
            if len(ipbody) < 8:
                continue
            sport, dport, ulen = struct.unpack_from(">HHH", ipbody, 0)
            if dport != 40204:
                continue
            out.append((ts, ipbody[8:ulen] if ulen >= 8 else ipbody[8:]))
        return out

    def run(self):
        try:
            pkts = self._udp_40204(self._read(self.path))
        except Exception as exc:
            self._status("读 pcap 失败：%s" % exc)
            log("读 pcap 失败：%s" % exc)
            return
        if not pkts:
            self._status("这个 pcap 里没有 40204 的 UDP")
            return
        t0 = pkts[0][0]
        self._status("回放 %d 个 UDP 数据报（%.1f s），%.1f 倍速"
                     % (len(pkts), pkts[-1][0] - t0, self.speed))
        while self.running:
            wall0 = time.time()
            for ts, data in pkts:
                if not self.running:
                    return
                dt = wall0 + (ts - t0) / self.speed - time.time()
                if dt > 0:
                    time.sleep(min(dt, 0.2))
                self._emit(_now_ns(), data, "pcap")
            if not self.loop:
                self._status("回放结束（用 --once 时只放一遍）")
                return
            time.sleep(0.3)


class DemoSource(Source):
    """合成数据：前方 3.6 m 墙 / 180° 走廊 43 m + 3.6 m 处衣物 / ±90° 侧墙。"""
    WALLS = [(75.9, 1.39, 70, 101), (241.8, 1.28, 248, 292),
             (203.7, 3.30, 164, 199), (178.0, 42.9, 160, 196)]

    def __init__(self, emit, rpm=1200.0, take_cmds=None):
        super().__init__(emit, take_cmds)
        self.name = "demo"
        self.rpm = rpm

    def run(self):
        self._status("合成数据：%.0f rpm（无硬件演示）" % self.rpm)
        grid = 1.031 + np.arange(179) * AZ_STEP
        seq = 0
        while self.running:
            t0 = time.time()
            azs, els, rs, amps = [], [], [], []
            for a in grid:
                deg = np.degrees(a) % 360.0
                for naz, rho, a0, a1 in self.WALLS:
                    dd = (deg - naz + 180) % 360 - 180
                    if a0 - naz - 8 <= dd <= a1 - naz + 8:
                        rr = rho / max(np.cos(np.radians(dd)), 0.25)
                        if rr < 62:
                            for k in ((1, 2) if rho > 20 else (1, 2, 3)):
                                azs.append(a + np.random.randn() * 0.0015)
                                els.append(np.arcsin(k / 64.0))
                                rs.append(rr + np.random.randn() * 0.02)
                                amps.append(max(60.0, 320 - 45 * k + np.random.randn() * 20))
                if np.random.rand() < 0.2:
                    azs.append(a)
                    els.append(np.arcsin(np.clip(np.random.randn() * 3 / 64, -0.9, 0.9)))
                    rs.append(np.random.uniform(5, 60))
                    amps.append(55 + abs(np.random.randn()) * 10)
            az = np.array(azs)
            self._emit(_now_ns(), dict(az=az, el=np.array(els), r=np.array(rs),
                                       c3=np.array(amps, dtype=np.float32),
                                       c4=np.array(amps, dtype=np.float32) * 0.3,
                                       c5=(az - grid[0]) * 7.7e3 + 8000.0,
                                       seq=seq & 0xFFFF, n=len(az), sum_ok=True), "demo")
            seq += 1
            dt = 60.0 / self.rpm - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)


# ================================================================= 界面
def make_window_class(args):
    from PySide6 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=False, background="k", foreground="w")

    _RAMP = [(0.00, (0, 0, 4)), (0.20, (40, 11, 84)), (0.40, (101, 21, 110)),
             (0.60, (159, 42, 99)), (0.75, (212, 72, 66)), (0.88, (245, 125, 21)),
             (1.00, (252, 255, 164))]
    CMAP = pg.ColorMap([p for p, _ in _RAMP], [c for _, c in _RAMP])
    NLEV = 16
    # 历史点渲染预算（2026-11-10 修卡死）：
    #   旧实现每来一帧就把"全部历史点"塞进一个 ScatterPlotItem 重画（setData O(N)，
    #   实测 10 万点 130 ms/帧），而量程一大所有点又都进入视野（paint O(可见点)），
    #   于是"量程 70 m + 累积圈数 400"直接把 GUI 线程占满 → 界面卡死。
    #   现在改成按块追加：每帧只重画最后一块，总量超预算就整块丢最老的。
    MAX_HIST_PTS = 80000      # 历史点总预算（≈15 ms 绘制，再多屏幕也分辨不出来）
    CHUNK_PTS = 4000          # 单个历史块最多多少点（= 每帧重画的上限）
    DRAW_MIN_DT = 0.025       # 重画最小间隔：数据再快也只按 40 Hz 刷

    class Worker(QtCore.QThread):
        """子线程：跑数据源、解码、把结果发信号给界面（绝不直接碰 widget）。"""
        frame = QtCore.Signal(object)
        note = QtCore.Signal(str)
        board = QtCore.Signal(object)
        ack = QtCore.Signal(object)

        def __init__(self, spec):
            super().__init__()
            self.spec = spec
            self.src = None
            self.can_cmd = False
            self._cmds = collections.deque()
            self._lock = threading.Lock()

        def send_cmd(self, op, payload=b""):
            with self._lock:
                self._cmds.append(bytes([op]) + payload)

        def _take_cmds(self):
            with self._lock:
                out = list(self._cmds)
                self._cmds.clear()
            return out

        def _emit(self, ts, payload, who):
            d = payload if isinstance(payload, dict) else decode_00f0(payload)
            if d is not None:
                self.frame.emit(d)

        def _note(self, text):
            self.note.emit(text)

        def _on_status(self, st):
            self.board.emit(st)

        def _on_ack(self, ack):
            self.ack.emit(ack)

        def run(self):
            try:
                self.src = build_source(self.spec, self._emit, self._note, self._take_cmds)
                if self.src is None:
                    return
                self.src.on_status = self._on_status
                self.src.on_ack = self._on_ack
                self.can_cmd = self.src.can_cmd
                self.note.emit("数据源：" + self.src.name)
                log("启动数据源 %s" % self.src.name)
                self.src.running = True
                self.src.run()
                self.note.emit("数据源已停止")
            except Exception:
                import traceback
                tb = traceback.format_exc()
                log("数据源异常：\n" + tb)
                self.note.emit("数据源异常：%s" % tb.strip().splitlines()[-1])

        def stop(self):
            if self.src:
                self.src.stop()
            self.wait(2500)

    def build_source(spec, emit, note, take_cmds):
        if spec.startswith("demo"):
            s = DemoSource(emit, take_cmds=take_cmds)
        elif spec.startswith("udp://"):
            bind, _, port = spec[6:].partition(":")
            s = UdpSource(bind or "0.0.0.0", int(port or 40204), emit, take_cmds)
        elif spec.startswith("tcp://"):
            host, _, port = spec[6:].partition(":")
            s = TcpSource(host or "127.0.0.1", int(port or 46000), emit, take_cmds,
                          wifi=(None if getattr(args, "no_wifi", False)
                                else (getattr(args, "wifi", "") or None)))
        elif spec.startswith("pcap:"):
            path = spec[5:]
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "..", path))
            s = PcapSource(path, emit, speed=args.speed, loop=not args.once,
                           take_cmds=take_cmds)
        else:
            note("不认识的 --src：%s" % spec)
            return None
        s.note = note
        return s

    class Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("T40 雷达点云 — 实时")
            self.resize(1380, 900)
            self.hist_chunks = []      # 历史点按块存：[{item,xs,ys,n,frames}]
            self.hist_pts = 0
            self.hist_frames = 0
            self.gui_ms = 0.0          # 上一帧界面侧耗时（诊断用）
            self._t_draw = 0.0
            self.t_status = 0.0        # 最近一次收到板卡 STATUS 的时刻
            self.t_conn = 0.0          # 本次连上的时刻
            self._warned_status = False
            self._drx = collections.deque()   # (时刻, radar_pkts) 近 5 s 采样
            self.paused = False
            self.rpm = 0.0
            self.n_frame = 0
            self.seq = -1
            self.ok = True
            self._cnt_frames = 0
            self._cnt_pts = 0
            self.fps = 0.0
            self.pps = 0.0
            self.n_total = 0
            self.t_last = 0.0
            self.link_ok = False
            self.last_err = "正在启动…"
            self.board_st = None
            self._build()
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(500)
            self.watch = QtCore.QTimer(self)
            self.watch.timeout.connect(self._watchdog)
            self.watch.start(1000)
            self.worker = Worker(args.src)
            self.worker.frame.connect(self._on_frame)
            self.worker.note.connect(self._note)
            self.worker.board.connect(self._on_board)
            self.worker.ack.connect(self._on_ack)
            self.worker.start()
            log("窗口启动，src=%s" % args.src)

        # ---------------- UI ----------------
        def _build(self):
            cw = QtWidgets.QWidget()
            self.setCentralWidget(cw)
            outer = QtWidgets.QVBoxLayout(cw)
            outer.setContentsMargins(6, 6, 6, 6)

            # 顶部状态条：绿=正常，黄=连上但没数据，红=错误
            self.banner = QtWidgets.QLabel("正在启动…")
            self.banner.setStyleSheet("background:#444;color:#fff;padding:6px;"
                                      "font-weight:bold;border-radius:3px")
            self.banner.setWordWrap(True)
            outer.addWidget(self.banner)

            lay = QtWidgets.QHBoxLayout()
            outer.addLayout(lay, 1)

            self.pw = pg.PlotWidget()
            self.pw.setAspectLocked(True)
            self.pw.setMouseEnabled(x=False, y=False)
            self.pw.hideAxis("left")
            self.pw.hideAxis("bottom")
            self.pw.setMenuEnabled(False)
            lay.addWidget(self.pw, 1)

            self.rings, self.ring_txt, self.spokes = [], [], []
            self.blind = None
            # 历史点不再用一个"大 item"：见 _hist_add（分块追加，每帧只重画最后一块）
            self.hist_brush = pg.mkBrush(70, 120, 190, 70)
            self.level_brush = []
            for i in range(NLEV):
                c = CMAP.map(i / (NLEV - 1.0), mode="qcolor")
                c.setAlpha(255)
                self.level_brush.append(pg.mkBrush(c))
            self.cur_item = pg.ScatterPlotItem(size=6, pen=None)
            self.pw.addItem(self.cur_item)
            self.radar_dot = pg.ScatterPlotItem(size=11, pen=pg.mkPen("w"),
                                               brush=pg.mkBrush(20, 20, 20))
            self.pw.addItem(self.radar_dot)
            self.radar_dot.setData([0], [0])
            self.lbl_front = pg.TextItem("前 0°", color=(205, 220, 240), anchor=(0.5, 0.5))
            self.lbl_back = pg.TextItem("后 180°", color=(205, 220, 240), anchor=(0.5, 0.5))
            self.lbl_r = pg.TextItem("右 90°", color=(205, 220, 240), anchor=(0.5, 0.5))
            self.lbl_l = pg.TextItem("左 270°", color=(205, 220, 240), anchor=(0.5, 0.5))
            for t in (self.lbl_front, self.lbl_back, self.lbl_r, self.lbl_l):
                self.pw.addItem(t)

            panel = QtWidgets.QWidget()
            panel.setFixedWidth(310)
            pl = QtWidgets.QVBoxLayout(panel)
            pl.setContentsMargins(2, 2, 2, 2)
            lay.addWidget(panel)

            g1 = QtWidgets.QGroupBox("显示")
            f1 = QtWidgets.QFormLayout(g1)
            self.sp_range = QtWidgets.QDoubleSpinBox()
            self.sp_range.setRange(2, 70)
            self.sp_range.setValue(8.0)
            self.sp_range.setSuffix(" m")
            self.sp_range.valueChanged.connect(self._redraw)
            f1.addRow("量程", self.sp_range)
            self.sp_thr = QtWidgets.QSpinBox()
            self.sp_thr.setRange(0, 2000)
            self.sp_thr.setValue(80)
            self.sp_thr.valueChanged.connect(self._redraw)
            f1.addRow("幅度阈值 c3", self.sp_thr)
            self.sp_hist = QtWidgets.QSpinBox()
            self.sp_hist.setRange(0, 400)
            self.sp_hist.setValue(30)
            self.sp_hist.setSuffix(" 圈")
            self.sp_hist.valueChanged.connect(self._clear)
            f1.addRow("累积圈数", self.sp_hist)
            self.ck_blind = QtWidgets.QCheckBox("标出 121° 无数据扇区")
            self.ck_blind.setChecked(True)
            self.ck_blind.stateChanged.connect(self._redraw)
            f1.addRow(self.ck_blind)
            # 画面朝向：默认整幅画面逆时针 180°（倒过来）+ 方位逆时针。
            # 注意这是"转画面"，不是"改方位数据"：点云/盲区/前后左右标注一起转。
            self.sp_rot = QtWidgets.QSpinBox()
            self.sp_rot.setRange(0, 359)
            self.sp_rot.setSingleStep(15)
            self.sp_rot.setWrapping(True)
            self.sp_rot.setSuffix(" °")
            self.sp_rot.setValue(int(getattr(args, "rot", ROT_DEFAULT)) % 360)
            self.sp_rot.setToolTip("画面逆时针旋转角度（默认 180° = 整幅画面倒过来）。\n"
                                   "历史点没法重投影，改朝向会清空已累积的点。")
            self.sp_rot.valueChanged.connect(self._reorient)
            f1.addRow("旋转", self.sp_rot)
            hb_rot = QtWidgets.QHBoxLayout()
            hb_rot.setSpacing(2)
            for label, val in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
                b = QtWidgets.QPushButton(label)
                b.setFixedWidth(52)
                b.clicked.connect(lambda _=False, v=val: self.sp_rot.setValue(v))
                hb_rot.addWidget(b)
            hb_rot.addStretch(1)
            f1.addRow(hb_rot)
            self.ck_ccw = QtWidgets.QCheckBox("方位逆时针")
            self.ck_ccw.setChecked(bool(getattr(args, "ccw", CCW_DEFAULT)))
            self.ck_ccw.stateChanged.connect(self._reorient)
            f1.addRow(self.ck_ccw)
            pl.addWidget(g1)

            g2 = QtWidgets.QGroupBox("雷达转速（走板卡 → CAN 0x1E80）")
            v2 = QtWidgets.QVBoxLayout(g2)
            hb = QtWidgets.QHBoxLayout()
            self.sp_rpm = QtWidgets.QSpinBox()
            self.sp_rpm.setRange(300, 1800)
            self.sp_rpm.setSingleStep(30)
            self.sp_rpm.setValue(1200)
            self.sp_rpm.setSuffix(" rpm")
            hb.addWidget(self.sp_rpm, 1)
            self.btn_rpm = QtWidgets.QPushButton("应用")
            self.btn_rpm.clicked.connect(lambda: self._set_rpm())
            hb.addWidget(self.btn_rpm)
            v2.addLayout(hb)
            hb2 = QtWidgets.QHBoxLayout()
            for label, val in (("600", 600), ("900", 900), ("1200", 1200),
                               ("1500", 1500), ("1800", 1800)):
                b = QtWidgets.QPushButton(label)
                b.setFixedWidth(46)
                b.clicked.connect(lambda _=False, v=val: (self.sp_rpm.setValue(v),
                                                          self._set_rpm()))
                hb2.addWidget(b)
            v2.addLayout(hb2)
            hb3 = QtWidgets.QHBoxLayout()
            b = QtWidgets.QPushButton("马达 开")
            b.clicked.connect(lambda: self._cmd(OP_MOTOR_ENABLE, b"", "马达使能",
                                                ask="给雷达马达使能？"))
            hb3.addWidget(b)
            b = QtWidgets.QPushButton("马达 关")
            b.clicked.connect(lambda: self._cmd(OP_MOTOR_DISABLE, b"", "马达停",
                                                ask="停掉雷达马达？停转后就没有点云了。"))
            hb3.addWidget(b)
            b = QtWidgets.QPushButton("停转")
            b.clicked.connect(lambda: self._cmd(OP_STOP, b"", "停转",
                                                ask="让雷达停转？停转后就没有点云了。"))
            hb3.addWidget(b)
            v2.addLayout(hb3)
            tip = QtWidgets.QLabel("提示：改转速会直接下发 CAN 0x1E80；\n"
                                   "若雷达自己在发转速，radar_min 的转发会覆盖它。")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#8a6d3b;font-size:10px")
            v2.addWidget(tip)
            self.lbl_ack = QtWidgets.QLabel("—")
            self.lbl_ack.setWordWrap(True)
            self.lbl_ack.setStyleSheet("color:#666")
            v2.addWidget(self.lbl_ack)
            pl.addWidget(g2)

            g3 = QtWidgets.QGroupBox("操作")
            hb = QtWidgets.QHBoxLayout(g3)
            self.btn_pause = QtWidgets.QPushButton("暂停")
            self.btn_pause.setCheckable(True)
            self.btn_pause.toggled.connect(self._pause)
            hb.addWidget(self.btn_pause)
            b = QtWidgets.QPushButton("清除")
            b.clicked.connect(lambda: self._clear())
            hb.addWidget(b)
            b = QtWidgets.QPushButton("截图")
            b.clicked.connect(lambda: self._shot())
            hb.addWidget(b)
            pl.addWidget(g3)

            self.info = QtWidgets.QPlainTextEdit()
            self.info.setReadOnly(True)
            self.info.setStyleSheet("font-family:Consolas,monospace;font-size:11px")
            pl.addWidget(self.info, 1)

            self.statusBar().showMessage("就绪；日志 %s" % LOG_PATH)
            self._grid()

        # ---------------- 朝向（画面旋转 / 方位方向） ----------------
        def _cw(self):
            return not self.ck_ccw.isChecked()

        def _rot(self):
            """画面逆时针旋转角（弧度）。"""
            return np.radians(float(self.sp_rot.value()))

        def _polar(self, az_deg, R):
            """把雷达方位角按当前朝向摆到画面坐标（和点云同一套变换）。"""
            a = np.radians(az_deg)
            s = a if self._cw() else -a
            s = s - self._rot()
            return float(np.sin(s) * R), float(np.cos(s) * R)

        def _reorient(self, *a):
            """改朝向：历史点是按老朝向算好的像素坐标，重投影不了 ⇒ 清掉重来。"""
            if self.hist_chunks or self.n_total or self.hist_pts:
                self._clear()
                self.statusBar().showMessage(
                    "朝向 %d° / %s：历史点已清空（旧点没法重投影）"
                    % (self.sp_rot.value(),
                       "逆时针" if self.ck_ccw.isChecked() else "顺时针"), 6000)
            self._redraw()

        # ---------------- 帧处理 ----------------
        def _on_frame(self, d):
            self._cnt_frames += 1
            self.t_last = time.time()
            self.link_ok = True
            az, el, r, c3 = d["az"], d["el"], d["r"], d["c3"]
            self.seq = d.get("seq", -1)
            self.ok = d.get("sum_ok", True)
            self.n_frame = d.get("n", 0)
            rp = rpm_from_c5(az, d["c5"])
            if rp:
                self.rpm = rp if self.rpm == 0 else self.rpm * 0.9 + rp * 0.1
            if self.paused:
                return
            x, y, _ = points_xy(az, el, r, clockwise=self._cw(), rot=self._rot())
            keep = c3 >= self.sp_thr.value()
            x, y, c3 = x[keep], y[keep], c3[keep]
            self._cnt_pts += len(x)
            self.n_total += len(x)
            t_gui = time.perf_counter()
            # 数据照收，但重画限速：每帧都重画的话，数据源比界面快时 GUI 线程会被占满
            # （点云 60k 点一次重画 ~13 ms，40 帧/s 就要 500 ms/s）。
            draw = (t_gui - self._t_draw) >= DRAW_MIN_DT
            if draw:
                self._t_draw = t_gui
                self._draw_current(x, y, c3)
            lim = int(self.sp_hist.value())
            if lim > 0:
                self._hist_add(x, y, lim, draw)
            elif self.hist_chunks:
                self._hist_clear()
            self.gui_ms = (time.perf_counter() - t_gui) * 1e3

        def _levels(self, c3):
            lo = float(self.sp_thr.value())
            t = np.clip((c3.astype(np.float32) - lo) / 300.0, 0.0, 0.999)
            return (t * NLEV).astype(np.int32)

        def _draw_current(self, x, y, c3):
            if not len(x):
                self.cur_item.setData(x=np.zeros(0), y=np.zeros(0), brush=[])
                return
            lv = self._levels(c3)
            self.cur_item.setData(x=x, y=y, brush=[self.level_brush[i] for i in lv])

        # ---- 历史点：分块追加（关键：每帧代价只和"这一块"有关，和总量无关） ----
        def _hist_new_chunk(self):
            item = pg.ScatterPlotItem(size=4, pen=None, brush=self.hist_brush)
            self.pw.addItem(item)
            ch = dict(item=item, xs=[], ys=[], n=0, frames=0)
            self.hist_chunks.append(ch)
            return ch

        def _hist_add(self, x, y, lim, flush=True):
            """把这一帧的点追加到历史的最后一块上。

            为什么不是一个大 item：pyqtgraph 的 ScatterPlotItem.setData 是 O(全量)，
            10 万点要 130 ms；每帧都做一遍 = GUI 线程永远追不上数据。
            flush=False 时只记账不重画（限速用），下一帧会把攒下的点一起画出来。
            """
            if not len(x):
                return
            # 块大小两头兼顾：总量 400 圈时用 CHUNK_PTS 装（块少 ⇒ 绘制项开销小），
            # 累积圈数很小时按 lim/8 帧封块（淘汰粒度细，不多留一截尾巴）。
            max_frames = max(1, lim // 8)
            ch = self.hist_chunks[-1] if self.hist_chunks else None
            if ch is None or ch["n"] >= CHUNK_PTS or ch["frames"] >= max_frames:
                ch = self._hist_new_chunk()
            ch["xs"].append(x)
            ch["ys"].append(y)
            ch["n"] += len(x)
            ch["frames"] += 1
            self.hist_pts += len(x)
            self.hist_frames += 1
            if flush:
                if len(ch["xs"]) == 1:
                    px, py = x, y
                else:
                    px = np.concatenate(ch["xs"])
                    py = np.concatenate(ch["ys"])
                ch["item"].setData(px, py)
            self._hist_trim(lim)

        def _hist_trim(self, lim):
            # 至少保留最新一块，否则小累积圈数时会把刚进来的数据也丢掉
            while len(self.hist_chunks) > 1 and (self.hist_frames > lim
                                                 or self.hist_pts > MAX_HIST_PTS):
                old = self.hist_chunks.pop(0)
                self.pw.removeItem(old["item"])
                self.hist_frames -= old["frames"]
                self.hist_pts -= old["n"]

        def _hist_clear(self):
            for ch in self.hist_chunks:
                self.pw.removeItem(ch["item"])
            self.hist_chunks = []
            self.hist_pts = 0
            self.hist_frames = 0

        # ---------------- 网格 ----------------
        def _grid(self):
            R = float(self.sp_range.value())
            self.pw.setXRange(-R * 1.18, R * 1.18, padding=0)
            self.pw.setYRange(-R * 1.18, R * 1.18, padding=0)
            for it in self.rings + self.ring_txt + self.spokes:
                self.pw.removeItem(it)
            if self.blind is not None:
                self.pw.removeItem(self.blind)
                self.blind = None
            self.rings, self.ring_txt, self.spokes = [], [], []
            step = 1 if R <= 10 else 5 if R <= 30 else 10
            g = np.linspace(0, 2 * np.pi, 181)
            for rr in np.arange(step, R + 1e-9, step):
                pen = pg.mkPen(70, 80, 95, 220, width=1,
                               style=(QtCore.Qt.PenStyle.DotLine
                                      if int(rr) % (2 * step)
                                      else QtCore.Qt.PenStyle.SolidLine))
                it = pg.PlotDataItem(np.sin(g) * rr, np.cos(g) * rr, pen=pen)
                self.pw.addItem(it)
                self.rings.append(it)
                t = pg.TextItem("%g m" % rr, color=(120, 135, 155), anchor=(0.5, 0.5))
                ta = np.radians(215) - self._rot()   # 只是装饰：跟着画面转，不参与方位镜像
                t.setPos(np.sin(ta) * rr, np.cos(ta) * rr)
                self.pw.addItem(t)
                self.ring_txt.append(t)
            for a in range(0, 360, 30):
                sx, sy = self._polar(a, R)
                it = pg.PlotDataItem([0, sx], [0, sy],
                                     pen=pg.mkPen(55, 62, 75, 170, width=1))
                self.pw.addItem(it)
                self.spokes.append(it)
            # 前/后/左/右 是"雷达的方位"，必须跟着画面朝向一起转：
            # 默认（180° 逆时针）下 0°（正前方）落在下方，标注也就在下方。
            for lbl, az in ((self.lbl_front, 0.0), (self.lbl_back, 180.0),
                            (self.lbl_r, 90.0), (self.lbl_l, 270.0)):
                lx, ly = self._polar(az, R * 1.10)
                lbl.setPos(lx, ly)
            if self.ck_blind.isChecked():
                th = np.linspace(BLIND[0], BLIND[1], 80)
                s = th if self._cw() else -th
                s = s - self._rot()
                pts = [QtCore.QPointF(0.0, 0.0)]
                pts += [QtCore.QPointF(float(np.sin(t) * R), float(np.cos(t) * R))
                        for t in s]
                pts.append(QtCore.QPointF(0.0, 0.0))
                self.blind = QtWidgets.QGraphicsPolygonItem(QtGui.QPolygonF(pts))
                self.blind.setBrush(pg.mkBrush(150, 150, 160, 50))
                self.blind.setPen(pg.mkPen(120, 125, 135, 120))
                self.pw.addItem(self.blind)

        def _redraw(self, *a):
            # 只重画网格：改量程/阈值/方位不该把历史点重画一遍（原来这里还调
            # _draw_hist，量程一大 + 累积圈数一大，光拖一下量程就卡住）
            self._grid()

        def _clear(self, *a):
            self._hist_clear()
            self.n_total = 0
            self.cur_item.setData(x=np.zeros(0), y=np.zeros(0), brush=[])

        def _pause(self, on):
            self.paused = bool(on)
            self.btn_pause.setText("继续" if on else "暂停")

        def _shot(self):
            fn = os.path.abspath(time.strftime("t40_cloud_%Y%m%d_%H%M%S.png"))
            self.pw.grab().save(fn)
            self._note("已保存 %s" % fn)

        # ---------------- 状态 / 看门狗 ----------------
        def _banner(self, color, text):
            bg = {"ok": "#1b7f3b", "warn": "#9a6b00", "err": "#a11d1d",
                  "idle": "#444"}[color]
            fg = "color:#fff"
            self.banner.setStyleSheet("background:%s;%s;padding:6px;font-weight:bold;"
                                      "border-radius:3px" % (bg, fg))
            self.banner.setText(text)

        def _note(self, text):
            if text.startswith("已连上"):
                self.link_ok = True
                self.t_status = 0.0        # 新连接重新计时
                self.t_conn = time.time()
                self._warned_status = False
            elif "失败" in text or "断开" in text or "异常" in text:
                self.link_ok = False
                if "数据源" not in text:
                    self.last_err = text
            if text.startswith("数据源："):
                self.statusBar().showMessage(text)
            else:
                self.statusBar().showMessage(text, 8000)
            if "异常" in text or "失败" in text:
                log("NOTE " + text)
            self._watchdog()          # 状态一变就刷，别等 1 s 定时器

        def _on_board(self, st):
            now = time.time()
            self.t_status = now
            prev = self.board_st
            st["kbps"] = 0.0
            if prev is not None:
                dt = now - getattr(self, "_board_t", now)
                if dt > 0.2:
                    d = st["radar_bytes"] - prev["radar_bytes"]
                    if d >= 0:
                        st["kbps"] = d / dt / 1024.0
                    self._board_t = now
            else:
                self._board_t = now
            self.board_st = st
            # radar_pkts 最近 ~5 s 涨了多少：用来区分"雷达没出点"和"链路在丢包"
            self._drx.append((now, st["radar_pkts"]))
            while len(self._drx) > 6 or (self._drx and now - self._drx[0][0] > 5.0):
                self._drx.popleft()

        def _on_ack(self, ack):
            op, res, msg = ack
            names = {OP_SET_RPM: "设置转速", OP_MOTOR_ENABLE: "马达使能",
                     OP_MOTOR_DISABLE: "马达停", OP_STOP: "停转"}
            nm = names.get(op, "op0x%02X" % op)
            ok = (res == 0)
            self.lbl_ack.setText("%s：%s%s" % (nm, "成功" if ok else "失败(result=%d)" % res,
                                              ("  " + msg) if msg else ""))
            self.lbl_ack.setStyleSheet("color:%s" % ("#1b7f3b" if ok else "#a11d1d"))
            self.statusBar().showMessage("命令应答：%s %s" % (nm, "成功" if ok else "失败"), 6000)
            log("CMD_ACK %s res=%d %s" % (nm, res, msg))

        def _watchdog(self):
            now = time.time()
            st = self.board_st
            if not self.link_ok:
                self._banner("err", "✖ %s" % self.last_err)
                return
            # 连上了却收不到 STATUS：板卡桥接的 build_status() 卡住（典型是 ath6kl
            # 楔死，iw/sysfs 调用不返回）。这时板卡没死透——ping 还通、CAN 还在走、
            # 桥接照样发 HELLO，但 STATUS 和雷达 UDP 都不会来，界面只会显示
            # "等待第一帧 0x00F0"，很容易误判成雷达没出点。所以单独报出来。
            ref = max(self.t_status, self.t_conn)
            if ref and now - ref > 5.0:
                ss = "%.0f s" % (now - ref) if self.t_status else "本次连接以来"
                if not self._warned_status:
                    self._warned_status = True
                    log("板卡 STATUS %s 没来：桥接 build_status() 卡住（ath6kl 楔死？）" % ss)
                self._banner("err",
                             "已连板卡，但 %s 收不到 STATUS —— 板卡侧桥接卡住了（典型是 "
                             "ath6kl 楔死，iw/sysfs 调用不返回）。特征：ping 通、CAN 还在走、"
                             "桥接照样发 HELLO，但 STATUS 和雷达 UDP 都不来。"
                             "处理：给板卡断电重启。" % ss)
                return
            # STATUS 在发，但板卡侧"采集新鲜度"不涨：新桥接会把"距离上次成功采集
            # 多少秒"放在 STATUS 最后 2 字节，卡住时 STATUS 照发、数字一路涨。
            age_st = int(st.get("status_age", 0)) if st else 0
            if age_st > 10:
                if not self._warned_status:
                    self._warned_status = True
                    log("板卡 STATUS 新鲜度 %d s：板卡侧采集卡住（iw/sysfs）" % age_st)
                self._banner("err",
                             "板卡侧状态采集卡住 %d s（STATUS 还在发，但读网卡/CAN 的调用不返回"
                             "——典型 ath6kl 楔死）。雷达数据也不会来。"
                             "板卡看门狗会自动重启它；不动的话就断电重启。" % age_st)
                return
            if self.board_st is not None and st.get("wifi") == 0:
                self._banner("warn",
                             "已连板卡，但板卡看不到雷达（wifi_connected=0）——雷达没上电/"
                             "没关联上板卡的 radar_wifi。板卡：radar_pkts=%d" % st["radar_pkts"])
                return
            if self.t_last == 0:
                # 还没解出任何 0x00F0：分清是"雷达没出点"还是"板卡→电脑在丢包"
                grew = 0
                if len(self._drx) >= 2:
                    grew = self._drx[-1][1] - self._drx[0][1]
                if st and grew > 0:
                    self._banner("err",
                                 "板卡在收雷达包（radar_pkts 5 s 内 +%d），但主机一个 0x00F0 都没收到 "
                                 "—— 板卡→电脑这段 WiFi 在丢大包（0x00F0 是几 KB~几十 KB 的 UDP，"
                                 "要切成几十个 IP 分片）。先试：把雷达 AP 换到 5G / 换信道，"
                                 "或者把电脑挪近。板卡：radar_pkts=%d dropped=%d wifi=%d"
                                 % (grew, st["radar_pkts"], st["dropped"], st["wifi"]))
                    return
                extra = ""
                if st:                      # 有 STATUS 才说明"确实是雷达没出点"
                    extra = ("  板卡：radar_pkts=%d dropped=%d wifi=%d"
                             % (st["radar_pkts"], st["dropped"], st["wifi"]))
                self._banner("warn", "已连板卡（STATUS 正常），等待第一帧 0x00F0 …%s" % extra)
                return
            age = now - self.t_last
            if age > 3.0:
                extra = ""
                if st:
                    extra = ("  板卡：radar_pkts=%d dropped=%d wifi=%d"
                             % (st["radar_pkts"], st["dropped"], st["wifi"]))
                self._banner("warn", "已连接，但 %.1f s 没有 0x00F0 —— 雷达没在出点（没转/没上电）%s"
                             % (age, extra))
                return
            if self.fps <= 0 and age > 0.8:
                self._banner("warn", "已连接，%.1f s 没有新帧（正在重连 / 雷达停转？）" % age)
                return
            self._banner("ok", "● 运行中 · %.1f 帧/s · %.0f 点/s · 转速 %.0f rpm · 校验和 %s"
                         % (self.fps, self.pps, self.rpm, "OK" if self.ok else "FAIL"))

        def _tick(self):
            self.fps = self._cnt_frames * 2.0
            self.pps = self._cnt_pts * 2.0
            self._cnt_frames = 0
            self._cnt_pts = 0
            st = self.board_st
            board_txt = "—"
            if st:
                board_txt = ("uptime %ds · CAN %d · radar_pkts %d · %.1f KB/s · dropped %d · "
                             "wifi %d" % (st["uptime_ms"] // 1000, st["can_rx"],
                                          st["radar_pkts"], st.get("kbps", 0.0),
                                          st["dropped"], st["wifi"]))
                ag = int(st.get("status_age", 0))
                if ag > 1:
                    board_txt += " · 采集已卡 %d s" % ag
            self.info.setPlainText(
                "帧率      %5.1f /s\n"
                "点/秒     %6.0f\n"
                "本帧点数  %5d\n"
                "累计点数  %7d\n"
                "累积圈数  %3d / %d\n"
                "累积点数  %6d / %d\n"
                "c0 序号   %5d\n"
                "校验和    %s\n"
                "实测转速  %4.0f rpm\n"
                "设定转速  %4d rpm\n"
                "量程      %4.0f m\n"
                "界面耗时  %5.1f ms/帧\n"
                "\n板卡 STATUS\n%s\n"
                "\n日志\n%s"
                % (self.fps, self.pps, self.n_frame, self.n_total,
                   self.hist_frames, self.sp_hist.value(),
                   self.hist_pts, MAX_HIST_PTS,
                   self.seq, "OK" if self.ok else "FAIL", self.rpm,
                   self.sp_rpm.value(), self.sp_range.value(), self.gui_ms,
                   board_txt, LOG_PATH))

        # ---------------- 命令 ----------------
        def _cmd(self, op, payload=b"", name="", ask=""):
            if ask and not getattr(args, "yes", False):
                r = QtWidgets.QMessageBox.question(
                    self, "确认", ask, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if r != QtWidgets.QMessageBox.Yes:
                    return
            if not self.worker.can_cmd:
                self.lbl_ack.setText("%s：当前数据源不支持控制（只有 tcp:// 板卡转发可以）" % name)
                self.lbl_ack.setStyleSheet("color:#9a6b00")
                return
            self.worker.send_cmd(op, payload)
            self.lbl_ack.setText("%s：已下发，等板卡应答…" % name)
            self.lbl_ack.setStyleSheet("color:#666")
            log("下发命令 %s op=0x%02X %s" % (name, op, payload.hex()))

        def _set_rpm(self):
            import struct as _s
            rpm = int(self.sp_rpm.value())
            if not getattr(args, "yes", False):
                r = QtWidgets.QMessageBox.question(
                    self, "确认改转速",
                    "把雷达转速设为 %d rpm？\n\n"
                    "这条命令直接走 CAN（0x1E80）打到雷达 ESC，生效约 1 秒，\n"
                    "过程中出点可能短暂中断。" % rpm,
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if r != QtWidgets.QMessageBox.Yes:
                    self.lbl_ack.setText("已取消")
                    self.lbl_ack.setStyleSheet("color:#666")
                    return
            self._cmd(OP_SET_RPM, _s.pack("<H", rpm), "设置转速 %d" % rpm)

        def closeEvent(self, ev):
            self.timer.stop()
            self.watch.stop()
            log("窗口关闭")
            if self.worker:
                self.worker.stop()
            ev.accept()

    return Window


# ================================================================= 自检
def selftest(path):
    """不开界面，把解码链路跑一遍：pcap → UDP → 0x00F0 → 点云。"""
    print("自检：%s" % path)
    frames = PcapSource._read(path)
    pkts = PcapSource._udp_40204(frames)
    good = bad = pts = 0
    seqs, ns, rpms = [], [], []
    t0 = time.time()
    for _ts, data in pkts:
        d = decode_00f0(data)
        if d is None:
            bad += 1
            continue
        good += 1
        pts += d["n"]
        seqs.append(d["seq"])
        ns.append(d["n"])
        r = rpm_from_c5(d["az"], d["c5"])
        if r:
            rpms.append(r)
        x, y, _ = points_xy(d["az"], d["el"], d["r"])
        assert np.isfinite(x).all() and np.isfinite(y).all()
    dt = time.time() - t0
    print("  UDP 数据报 %d，0x00F0 %d 帧，其它 %d" % (len(pkts), good, bad))
    print("  记录 %d 条，每帧 %.1f 条（%d…%d）"
          % (pts, pts / max(good, 1), min(ns) if ns else 0, max(ns) if ns else 0))
    if seqs:
        d = np.diff(seqs) % 65536
        print("  帧序号 +1 的比例 %.2f%%" % (100.0 * (d == 1).mean()))
    if rpms:
        print("  c5 反推转速 p10/p50/p90 = %.0f / %.0f / %.0f rpm"
              % tuple(np.percentile(rpms, [10, 50, 90])))
    print("  纯 Python 解码 %.0f 帧/s、%.0f 点/s，耗时 %.2f s"
          % (good / dt, pts / dt, dt))
    print("  自检通过")


def main():
    ap = argparse.ArgumentParser(description="T40 雷达 0x00F0 点云实时显示")
    ap.add_argument("--src", default="tcp://192.168.9.1:46000",
                    help="tcp://host:port（默认板卡）| udp://bind:port | pcap:file | demo")
    ap.add_argument("--wifi", default="t40_link",
                    help="连不上板卡时自动加入的 WiFi（默认 t40_link，空串关闭）")
    ap.add_argument("--no-wifi", action="store_true", help="不要在连不上时自动切 WiFi")
    ap.add_argument("--speed", type=float, default=1.0, help="pcap 回放倍速")
    ap.add_argument("--once", action="store_true", help="pcap 只放一遍")
    ap.add_argument("--yes", action="store_true", help="改转速/马达前不再弹确认")
    ap.add_argument("--rot", type=int, default=ROT_DEFAULT,
                    help="画面逆时针旋转角度（默认 %d°，180=整幅画面倒过来）" % ROT_DEFAULT)
    ap.add_argument("--cw", action="store_true", default=not CCW_DEFAULT,
                    help="方位顺时针（默认逆时针）")
    ap.add_argument("--selftest", metavar="PCAP", help="不开界面，只跑解码链路")
    args = ap.parse_args()
    args.ccw = not args.cw            # 面板「方位逆时针」的初值

    log("=== 启动 argv=%s" % " ".join(sys.argv[1:]))

    if args.selftest:
        selftest(args.selftest)
        return 0

    try:
        from PySide6 import QtWidgets, QtGui
        import pyqtgraph  # noqa: F401
    except ImportError as exc:
        fatal("缺少依赖：%s\n\n请先运行：\n    pip install PySide6 pyqtgraph numpy" % exc)
        return 1

    def hook(etype, value, tb):
        import traceback
        txt = "".join(traceback.format_exception(etype, value, tb))
        log("未捕获异常：\n" + txt)
        fatal("程序内部异常（已写入日志 %s）：\n\n%s" % (LOG_PATH, txt[-1200:]))

    sys.excepthook = hook

    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setStyle("Fusion")
        f = QtGui.QFont()
        f.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC",
                       "Noto Sans CJK JP", "sans-serif"])
        f.setPointSize(9)
        app.setFont(f)
        win = make_window_class(args)()
        win.show()
    except Exception:
        import traceback
        fatal("界面启动失败（已写入日志 %s）：\n\n%s" % (LOG_PATH, traceback.format_exc()[-1500:]))
        return 1

    if args.src.startswith("demo"):
        print("提示：当前是 demo（合成数据），接真雷达去掉 --src demo")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
