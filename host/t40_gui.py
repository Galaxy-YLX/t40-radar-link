#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t40_gui.py —— T40 雷达上位机

连接板卡（TCP 46000），接收 CAN 遥测和雷达 UDP 业务数据，实时显示并下发控制命令。
协议见 protocol/PROTOCOL.md。

依赖: pip install PySide6 pyqtgraph

用法:
    python t40_gui.py
    python t40_gui.py --host 192.168.9.1 --port 46000 --auto
"""
import argparse
import collections
import struct
import sys
import time
import zlib

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

try:
    import duml_v2 as dv2
except ImportError:
    dv2 = None

# ------------------------------------------------------------------ 协议常量
MAGIC = b"T40R"
VERSION = 1
HDR = 24

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
OP_PING = 0x08

CAN_RX_ID = 0x0A3


def encode_frame(mtype, payload, seq=0):
    n = len(payload)
    buf = bytearray(HDR + n + 4)
    buf[0:4] = MAGIC
    buf[4] = VERSION
    buf[5] = mtype
    struct.pack_into("<H", buf, 6, 0)
    struct.pack_into("<I", buf, 8, seq & 0xFFFFFFFF)
    struct.pack_into("<Q", buf, 12, int(time.monotonic() * 1e9))
    struct.pack_into("<I", buf, 20, n)
    buf[HDR:HDR + n] = payload
    struct.pack_into("<I", buf, HDR + n, zlib.crc32(bytes(buf[4:HDR + n])) & 0xFFFFFFFF)
    return bytes(buf)


# ------------------------------------------------------------------ DUML 重组
class DumlAssembler(object):
    """把 CAN 8 字节分片重组成 DUML v1 包。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        out = []
        if data and data[0] == 0x55:
            if len(self.buf) >= 13:
                pkt = self._parse(bytes(self.buf))
                if pkt:
                    out.append(pkt)
            self.buf = bytearray(data)
        elif self.buf:
            self.buf += data
            if len(self.buf) >= 2:
                total = self.buf[1] | ((self.buf[2] & 0x03) << 8) if len(self.buf) >= 3 else 0
                if total and len(self.buf) >= total:
                    pkt = self._parse(bytes(self.buf[:total]))
                    if pkt:
                        out.append(pkt)
                    self.buf = bytearray(self.buf[total:])
        return out

    @staticmethod
    def _parse(d):
        if len(d) < 13 or d[0] != 0x55:
            return None
        total = d[1] | ((d[2] & 0x03) << 8)
        if total != len(d):
            return None
        return {"msg_id": (d[9] << 8) | d[10], "payload": d[11:-2]}


# ------------------------------------------------------------------ 网络线程
class NetThread(QtCore.QThread):
    frame_in = QtCore.Signal(int, bytes, "qint64")   # type, payload, ts_ns(64位，不能用 int)
    connected = QtCore.Signal(bool, str)

    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.txq = collections.deque()
        self.lock = QtCore.QMutex()

    def run(self):
        import socket
        self.running = True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
            self.sock.settimeout(0.2)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connected.emit(True, "%s:%d" % (self.host, self.port))
        except Exception as exc:
            self.connected.emit(False, str(exc))
            self.running = False
            return

        buf = bytearray()
        last_hb = 0.0
        while self.running:
            # 发送
            self.lock.lock()
            pending = list(self.txq)
            self.txq.clear()
            self.lock.unlock()
            for b in pending:
                try:
                    self.sock.sendall(b)
                except Exception:
                    self.running = False
                    break
            # 心跳
            t = time.time()
            if t - last_hb >= 1.0:
                last_hb = t
                try:
                    self.sock.sendall(encode_frame(T_HEARTBEAT, struct.pack("<I", int(t) & 0xFFFFFFFF)))
                except Exception:
                    self.running = False
                    break
            # 接收
            try:
                data = self.sock.recv(65536)
            except Exception:
                continue
            if not data:
                self.connected.emit(False, "对端关闭连接")
                self.running = False
                break
            buf += data
            while len(buf) >= HDR:
                if bytes(buf[0:4]) != MAGIC:
                    buf.clear()
                    break
                n = struct.unpack_from("<I", buf, 20)[0]
                if len(buf) < HDR + n + 4:
                    break
                body = bytes(buf[4:HDR + n])
                crc = struct.unpack_from("<I", buf, HDR + n)[0]
                if (zlib.crc32(body) & 0xFFFFFFFF) == crc:
                    self.frame_in.emit(buf[5], bytes(buf[HDR:HDR + n]),
                                       struct.unpack_from("<Q", buf, 12)[0])
                del buf[0:HDR + n + 4]
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def send(self, data):
        self.lock.lock()
        self.txq.append(data)
        self.lock.unlock()

    def stop(self):
        self.running = False


# ------------------------------------------------------------------ 主窗口
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, host, port):
        super().__init__()
        self.setWindowTitle("T40 雷达上位机")
        self.resize(1280, 820)
        self.net = None
        self.asm = DumlAssembler()

        self.stat = {"can": 0, "udp": 0, "udp_bytes": 0, "dropped": 0,
                     "wifi": 0, "rssi": 0, "can_state": 0, "ap_ready": 0}
        self.board_status = {}
        self.rate_win = collections.deque()
        self.t_hist = collections.deque(maxlen=600)
        self.rpm_hist = collections.deque(maxlen=600)
        self.gyro_hist = collections.deque(maxlen=600)
        self.last_udp_ts = 0.0
        self.udp_rate = 0.0
        # 板卡→界面 延迟：板端 ts 是 monotonic，PC 也是 monotonic，两者速率一致，
        # 差值里只差一个固定偏移。用最近 10 秒内的最小值当零点，其余帧相对它的
        # 超出量就是排队+网络+界面处理的延迟。滑动窗口取最小值可自动补偿时钟漂移。
        self.lat_raw = collections.deque()
        self.lat_win = collections.deque(maxlen=120)
        self.points = []          # 当前帧点云 [(x,y,z)]
        self.pc_buf = collections.deque()   # 最近 N 秒的点 [(t,x,y,z)]
        self.pc_window = 1.0      # 点云累积窗（秒），越小越跟手
        self.pc_palette = self._make_palette()
        self.pc_total = 0         # 累计解出的点数
        self.pc_frames = 0        # 带点的帧数
        self.pc_sub = {}          # subtype 计数

        self._build_ui(host, port)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        # 30Hz：雷达自己出帧约 31 帧/秒，再快也没新数据；200ms 时界面最多白等 200ms
        self.timer.start(33)

    @staticmethod
    def _make_palette(n=32):
        """预生成高度色带，避免每帧为每个点新建 QBrush（几百个/次 × 30Hz 很吃 CPU）。"""
        pal = []
        for i in range(n):
            t = i / float(n - 1)
            pal.append(pg.mkBrush(int(255 * t), int(200 * (1 - t)), 255, 200))
        return pal

    # -------------------------------------------------- UI
    def _build_ui(self, host, port):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # 顶部连接栏
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("板卡 IP"))
        self.ed_host = QtWidgets.QLineEdit(host)
        self.ed_host.setFixedWidth(140)
        top.addWidget(self.ed_host)
        top.addWidget(QtWidgets.QLabel("端口"))
        self.ed_port = QtWidgets.QLineEdit(str(port))
        self.ed_port.setFixedWidth(70)
        top.addWidget(self.ed_port)
        self.btn_conn = QtWidgets.QPushButton("连接")
        self.btn_conn.clicked.connect(self.toggle_conn)
        top.addWidget(self.btn_conn)
        self.lbl_state = QtWidgets.QLabel("未连接")
        self.lbl_state.setStyleSheet("color:#c0392b;font-weight:bold")
        top.addWidget(self.lbl_state)
        top.addStretch(1)
        root.addLayout(top)

        body = QtWidgets.QHBoxLayout()
        root.addLayout(body, 1)

        # 左：控制 + 状态
        left = QtWidgets.QVBoxLayout()
        body.addLayout(left, 0)

        gb_ctrl = QtWidgets.QGroupBox("雷达控制")
        cl = QtWidgets.QVBoxLayout(gb_ctrl)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("转速"))
        self.sp_rpm = QtWidgets.QSpinBox()
        self.sp_rpm.setRange(0, 1800)
        self.sp_rpm.setValue(1200)
        self.sp_rpm.setSuffix(" rpm")
        row.addWidget(self.sp_rpm)
        cl.addLayout(row)
        self.sl_rpm = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sl_rpm.setRange(0, 1800)
        self.sl_rpm.setValue(1200)
        self.sl_rpm.valueChanged.connect(self.sp_rpm.setValue)
        self.sp_rpm.valueChanged.connect(self.sl_rpm.setValue)
        cl.addWidget(self.sl_rpm)

        # 点云累积窗：窗越长越完整但越"拖影"，窗越短越跟手
        row_win = QtWidgets.QHBoxLayout()
        row_win.addWidget(QtWidgets.QLabel("点云窗口"))
        self.sp_win = QtWidgets.QDoubleSpinBox()
        self.sp_win.setRange(0.2, 5.0)
        self.sp_win.setSingleStep(0.1)
        self.sp_win.setDecimals(1)
        self.sp_win.setValue(self.pc_window)
        self.sp_win.setSuffix(" s")
        self.sp_win.valueChanged.connect(self._set_window)
        row_win.addWidget(self.sp_win)
        row_win.addStretch(1)
        cl.addLayout(row_win)

        b1 = QtWidgets.QHBoxLayout()
        self.btn_set = QtWidgets.QPushButton("设置转速")
        self.btn_set.clicked.connect(self.cmd_set_rpm)
        b1.addWidget(self.btn_set)
        self.btn_stop = QtWidgets.QPushButton("停转")
        self.btn_stop.clicked.connect(lambda: self.cmd(OP_STOP))
        b1.addWidget(self.btn_stop)
        cl.addLayout(b1)

        b2 = QtWidgets.QHBoxLayout()
        self.btn_en = QtWidgets.QPushButton("使能")
        self.btn_en.clicked.connect(lambda: self.cmd(OP_MOTOR_ENABLE))
        b2.addWidget(self.btn_en)
        self.btn_dis = QtWidgets.QPushButton("禁用")
        self.btn_dis.clicked.connect(lambda: self.cmd(OP_MOTOR_DISABLE))
        b2.addWidget(self.btn_dis)
        cl.addLayout(b2)
        left.addWidget(gb_ctrl)

        gb_st = QtWidgets.QGroupBox("状态")
        sl = QtWidgets.QGridLayout(gb_st)
        self.lbl = {}
        rows = [("rpm", "转速反馈"), ("lat", "板卡→界面"), ("can", "CAN 帧"),
                ("udp", "雷达包"), ("udp_rate", "包速率"), ("udp_kb", "雷达流量"),
                ("wifi", "雷达关联"), ("rssi", "信号"), ("can_state", "CAN 状态"),
                ("dropped", "丢弃")]
        for i, (k, name) in enumerate(rows):
            sl.addWidget(QtWidgets.QLabel(name), i // 2, (i % 2) * 2)
            v = QtWidgets.QLabel("--")
            v.setStyleSheet("font-weight:bold")
            sl.addWidget(v, i // 2, (i % 2) * 2 + 1)
            self.lbl[k] = v
        left.addWidget(gb_st)
        left.addStretch(1)

        # 右：图表
        right = QtWidgets.QVBoxLayout()
        body.addLayout(right, 1)
        pg.setConfigOptions(antialias=True, background="#101418", foreground="#cfd8dc")

        self.p_rpm = pg.PlotWidget(title="转速反馈 (rpm)")
        self.p_rpm.showGrid(x=True, y=True, alpha=0.25)
        self.p_rpm.addLegend(offset=(10, 10))
        self.c_rpm = self.p_rpm.plot(pen=pg.mkPen("#4fc3f7", width=2), name="rpm")
        right.addWidget(self.p_rpm, 2)

        # 点云（俯视图：x 向前，y 向右，颜色表示高度）
        self.p_pc = pg.PlotWidget(title="雷达点云（俯视 x-y，颜色=高度 z）")
        self.p_pc.showGrid(x=True, y=True, alpha=0.2)
        self.p_pc.setAspectLocked(True)
        self.p_pc.setXRange(-50, 50)
        self.p_pc.setYRange(-50, 50)
        self.p_pc.addLine(x=0, pen=pg.mkPen("#37474f", width=1))
        self.p_pc.addLine(y=0, pen=pg.mkPen("#37474f", width=1))
        self.sc_pc = pg.ScatterPlotItem(size=5, pen=None)
        self.p_pc.addItem(self.sc_pc)
        right.addWidget(self.p_pc, 3)

        self.p_rate = pg.PlotWidget(title="雷达数据包速率 (包/秒)")
        self.p_rate.showGrid(x=True, y=True, alpha=0.25)
        self.c_rate = self.p_rate.plot(pen=pg.mkPen("#ab47bc", width=2))
        right.addWidget(self.p_rate, 1)

        # 底部日志
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(110)
        self.log.setStyleSheet("font-family:Consolas,monospace;font-size:12px")
        root.addWidget(self.log)

    # -------------------------------------------------- 连接
    def toggle_conn(self):
        if self.net and self.net.running:
            self.net.stop()
            self.net.wait(1500)
            self.net = None
            self.btn_conn.setText("连接")
            self._set_state(False, "未连接")
            return
        try:
            port = int(self.ed_port.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "参数错误", "端口必须是数字")
            return
        self.net = NetThread(self.ed_host.text().strip(), port)
        self.net.frame_in.connect(self.on_frame)
        self.net.connected.connect(self._set_state)
        self.net.start()
        self.btn_conn.setText("断开")

    def _set_state(self, ok, msg):
        if ok:
            self.lbl_state.setText("已连接 " + msg)
            self.lbl_state.setStyleSheet("color:#27ae60;font-weight:bold")
            self._log("已连接 " + msg)
        else:
            self.lbl_state.setText("未连接")
            self.lbl_state.setStyleSheet("color:#c0392b;font-weight:bold")
            self._log("连接失败/断开: " + msg)
            self.btn_conn.setText("连接")

    def _log(self, text):
        self.log.appendPlainText("[%s] %s" % (time.strftime("%H:%M:%S"), text))

    # -------------------------------------------------- 命令
    def cmd(self, op, args=b""):
        if not (self.net and self.net.running):
            self._log("未连接，命令未发送")
            return
        self.net.send(encode_frame(T_CMD, bytes([op]) + args))
        self._log("下发命令 op=0x%02X args=%s" % (op, args.hex()))

    def cmd_set_rpm(self):
        self.cmd(OP_SET_RPM, struct.pack("<H", self.sp_rpm.value()))

    def _set_window(self, v):
        self.pc_window = float(v)
        # 立刻生效，不然旧的长窗还会拖一阵
        while self.pc_buf and time.time() - self.pc_buf[0][0] > self.pc_window:
            self.pc_buf.popleft()

    # -------------------------------------------------- 收帧
    def on_frame(self, mtype, payload, ts_ns):
        if mtype == T_CAN:
            self.stat["can"] += 1
            can_id, dlc, _flags = struct.unpack_from("<IBB", payload, 0)
            if can_id == CAN_RX_ID:
                for pkt in self.asm.feed(payload[6:6 + dlc]):
                    self.on_duml(pkt)
        elif mtype == T_RADAR_UDP:
            self.stat["udp"] += 1
            self.stat["udp_bytes"] += max(0, len(payload) - 2)
            self.last_udp_ts = time.time()
            self._note_latency(ts_ns)
            if dv2 is not None and len(payload) > 2:
                fr = dv2.parse_v2(payload[2:])
                if fr and fr["msg_id"] == 0x1E61:
                    try:
                        st, entries = dv2.decode_perception(fr["payload"])
                    except ValueError:
                        return
                    self.pc_sub[st] = self.pc_sub.get(st, 0) + 1
                    if entries:
                        now = time.time()
                        self.points = [(e["x"], e["y"], e["z"]) for e in entries]
                        for e in entries:
                            self.pc_buf.append((now, e["x"], e["y"], e["z"]))
                        self.pc_total += len(entries)
                        self.pc_frames += 1
        elif mtype == T_STATUS:
            if len(payload) >= 40:
                v = struct.unpack("<IIIIIIIBb6sBBH", payload[:40])
                # v[0..6]=7个uint32  v[7]=wifi v[8]=rssi v[9]=mac v[10]=can_state v[11]=ap_ready
                self.stat["wifi"] = v[7]
                self.stat["rssi"] = v[8]
                self.stat["can_state"] = v[10]
                self.stat["ap_ready"] = v[11]
                self.stat["dropped"] = v[6]
                self.board_status = {"can_rx": v[1], "can_tx": v[2], "udp": v[4], "bytes": v[5]}
        elif mtype == T_LOG:
            if payload:
                lvl = payload[0]
                txt = payload[1:].decode("utf-8", "replace")
                self._log(("[错误] " if lvl == 2 else "[警告] " if lvl == 1 else "") + txt)
        elif mtype == T_CMD_ACK:
            if len(payload) >= 2:
                op, res = payload[0], payload[1]
                msg = payload[6:].decode("utf-8", "replace") if len(payload) > 6 else ""
                self._log("命令 0x%02X %s %s" % (op, "成功" if res == 0 else "失败(%d)" % res, msg))

    def _note_latency(self, ts_ns):
        """记录一帧从板卡 push 到 PC 收到的时间差（扣除固定时钟偏移）。"""
        now = time.monotonic()
        d = now - ts_ns / 1e9
        self.lat_raw.append((now, d))
        while self.lat_raw and now - self.lat_raw[0][0] > 10.0:
            self.lat_raw.popleft()
        base = min(x[1] for x in self.lat_raw)
        self.lat_win.append(max(0.0, (d - base) * 1000.0))

    def on_duml(self, pkt):
        mid, p = pkt["msg_id"], pkt["payload"]
        if mid == 0x1E0F and len(p) >= 32:
            rpm = struct.unpack_from("<H", p, 29)[0]
            self.t_hist.append(time.time())
            self.rpm_hist.append(rpm)
        elif mid == 0x1E0B and len(p) >= 26:
            g = struct.unpack_from("<3f", p, 14)
            self.gyro_hist.append(g)

    # -------------------------------------------------- 刷新
    def _tick(self):
        now = time.time()
        self.rate_win.append((now, self.stat["udp"]))
        while self.rate_win and now - self.rate_win[0][0] > 2.0:
            self.rate_win.popleft()
        if len(self.rate_win) >= 2 and now - self.rate_win[0][0] > 0.5:
            dt = now - self.rate_win[0][0]
            self.udp_rate = (self.stat["udp"] - self.rate_win[0][1]) / dt

        self.lbl["rpm"].setText("%d rpm" % (self.rpm_hist[-1] if self.rpm_hist else 0))
        if self.lat_win:
            s = sorted(self.lat_win)
            med = s[len(s) // 2]
            p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
            self.lbl["lat"].setText("%.0f ms (p90 %.0f)" % (med, p90))
            self.lbl["lat"].setStyleSheet(
                "font-weight:bold;color:%s" % ("#27ae60" if med < 30 else
                                               "#f39c12" if med < 100 else "#c0392b"))
        else:
            self.lbl["lat"].setText("--")
        self.lbl["can"].setText("%d" % self.stat["can"])
        self.lbl["udp"].setText("%d" % self.stat["udp"])
        self.lbl["udp_rate"].setText("%.0f /s" % self.udp_rate)
        self.lbl["udp_kb"].setText("%.1f KB/s" % (self.udp_rate * 0 + self._kb_rate()))
        self.lbl["wifi"].setText("已连接" if self.stat["wifi"] else "未连接")
        self.lbl["wifi"].setStyleSheet(
            "font-weight:bold;color:%s" % ("#27ae60" if self.stat["wifi"] else "#c0392b"))
        # ath6kl 在 AP 模式下不实现 station dump，拿不到 RSSI，显示 N/A
        self.lbl["rssi"].setText("%d dBm" % self.stat["rssi"] if self.stat["rssi"] else "N/A")
        self.lbl["can_state"].setText(["ERROR-ACTIVE", "PASSIVE", "BUS-OFF"][
            min(self.stat["can_state"], 2)])
        self.lbl["dropped"].setText("%d" % self.stat["dropped"])

        if self.t_hist:
            t0 = self.t_hist[0]
            xs = [t - t0 for t in self.t_hist]
            self.c_rpm.setData(xs, list(self.rpm_hist))
        # 点云：累积最近 pc_window 秒（雷达在转，多帧叠起来才是完整一圈）
        now2 = time.time()
        while self.pc_buf and now2 - self.pc_buf[0][0] > self.pc_window:
            self.pc_buf.popleft()
        if self.pc_buf:
            pts = list(self.pc_buf)
            xs = [p[1] for p in pts]
            ys = [p[2] for p in pts]
            zs = [p[3] for p in pts]
            zmin, zmax = min(zs), max(zs)
            rng = max(zmax - zmin, 0.5)
            pal = self.pc_palette
            last = len(pal) - 1
            brushes = [pal[min(last, max(0, int((z - zmin) / rng * last)))] for z in zs]
            self.sc_pc.setData(xs, ys, brush=brushes)
            self.p_pc.setTitle(
                "雷达点云（俯视 x-y，颜色=高度 z）  %.1fs窗 %d 点 / 当前帧 %d / 累计 %d"
                % (self.pc_window, len(pts), len(self.points), self.pc_total))
        # 速率曲线
        self.rate_pts = getattr(self, "rate_pts", collections.deque(maxlen=300))
        self.rate_pts.append((now, self.udp_rate))
        if self.rate_pts:
            x0 = self.rate_pts[0][0]
            self.c_rate.setData([p[0] - x0 for p in self.rate_pts], [p[1] for p in self.rate_pts])

    def _kb_rate(self):
        # 粗略估算：按平均包长 130 字节
        return self.udp_rate * 130 / 1024.0

    def closeEvent(self, ev):
        if self.net:
            self.net.stop()
            self.net.wait(1000)
        super().closeEvent(ev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.9.1",
                    help="板卡地址（t40_link 网段；老以太网方案是 192.168.1.254）")
    ap.add_argument("--port", type=int, default=46000)
    ap.add_argument("--auto", action="store_true", help="启动后自动连接")
    a = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow(a.host, a.port)
    w.show()
    if a.auto:
        QtCore.QTimer.singleShot(600, w.toggle_conn)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
