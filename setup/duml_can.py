#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
duml_can.py —— T40 雷达 ESC 的 DUML over CAN 工具

协议参数来自 leommxj/dji-agras-radar 的 radar_min_client.py，并用本地抓到的
66 个真实报文验证过 CRC。

  CAN 帧      : 标准帧, 8 字节分片, 长包按 8 字节切片连续发
  主机 -> ESC : can_id 0x0A2
  ESC -> 主机 : can_id 0x0A3

  DUML v1 帧结构 (总长 total_len):
    [0]      0x55
    [1]      total_len 低 8 位
    [2]      0x04 | (total_len >> 8) & 0x03
    [3]      CRC8(frame[0:3])            init=0x77  poly=0x8C(反射)
    [4]      sender      我们=0x78  ESC=0x18
    [5]      receiver    我们=0x18  ESC=0x78
    [6:8]    seq uint16 LE
    [8]      flags
    [9]      cmd_set
    [10]     cmd_id
    [11:-2]  payload
    [-2:]    CRC16(frame[:-2]) LE        init=0x3692 poly=0x8408(反射)

用法:
    python3 duml_can.py listen                    # 只听，解出 ESC 上报
    python3 duml_can.py speed 60                  # 设转速 60rpm（单发）
    python3 duml_can.py speed 900 --repeat 60 --interval 0.2   # 持续 12 秒
    python3 duml_can.py stop
    python3 duml_can.py enable / disable
    python3 duml_can.py hms 1
    python3 duml_can.py raw 1E 80 3C00            # 自定义 cmd_set/cmd_id/payload(hex)
"""
import argparse
import socket
import struct
import sys
import time

CAN_TX = 0x0A2
CAN_RX = 0x0A3
HDR_LEN = 11
TAIL_LEN = 2

SENDER_HOST = 0x78
RECEIVER_ESC = 0x18

MSG_MOTOR_ENABLE = 0x1E09
MSG_MOTOR_DISABLE = 0x1E0A
MSG_IMU_PACK = 0x1E0B
MSG_BOTTOM_PACK = 0x1E0F
MSG_MOTOR_SPEED = 0x1E80
MSG_MOTOR_STOP = 0x1E82
MSG_HMS_SWITCH = 0x1E83
MSG_STATE_QUERY = 0x1EFE

NAMES = {
    0x1E09: "motor_enable", 0x1E0A: "motor_disable",
    0x1E0B: "radar_imu_pack", 0x1E0F: "motor_bottom_pack",
    0x1E21: "flight_pos", 0x1E22: "flight_ctrl",
    0x1E32: "avoidance_alt", 0x1E62: "perception_v2",
    0x1E80: "motor_speed", 0x1E81: "motor_source_pos",
    0x1E82: "motor_stop", 0x1E83: "hms_switch",
    0x1EF9: "motor_state_f9", 0x1EFE: "motor_state_query",
    0x0032: "activation", 0x0E01: "diagnostic",
}


def hx(data):
    """Python 3.6 兼容的十六进制打印（bytes.hex(sep) 要 3.8+）"""
    return " ".join("%02x" % b for b in data)


# ------------------------------------------------------------------ 校验
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


# ------------------------------------------------------------------ 组包
def build_frame(msg_id, payload=b"", seq=0, sender=SENDER_HOST,
                receiver=RECEIVER_ESC, flags=0x00):
    total = HDR_LEN + len(payload) + TAIL_LEN
    f = bytearray(total)
    f[0] = 0x55
    f[1] = total & 0xFF
    f[2] = 0x04 | ((total >> 8) & 0x03)
    f[3] = crc8(f[:3])
    f[4] = sender & 0xFF
    f[5] = receiver & 0xFF
    struct.pack_into("<H", f, 6, seq & 0xFFFF)
    f[8] = flags & 0xFF
    f[9] = (msg_id >> 8) & 0xFF
    f[10] = msg_id & 0xFF
    f[11:11 + len(payload)] = payload
    struct.pack_into("<H", f, total - 2, crc16(f[:-2]))
    return bytes(f)


def parse_frame(data):
    if len(data) < HDR_LEN + TAIL_LEN or data[0] != 0x55:
        return None
    if (data[2] & 0xFC) != 0x04:
        return None
    total = data[1] | ((data[2] & 0x03) << 8)
    if total != len(data):
        return None
    if crc8(data[:3]) != data[3]:
        return None
    if struct.unpack_from("<H", data, total - 2)[0] != crc16(data[:-2]):
        return None
    return {
        "sender": data[4], "receiver": data[5],
        "seq": struct.unpack_from("<H", data, 6)[0],
        "flags": data[8],
        "msg_id": (data[9] << 8) | data[10],
        "payload": data[11:-2],
    }


# ------------------------------------------------------------------ CAN
class Can:
    def __init__(self, iface="can0"):
        self.s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.s.bind((iface,))

    def send_chunk(self, can_id, data):
        self.s.send(struct.pack("<IB3x8s", can_id, len(data), data.ljust(8, b"\0")))

    def send_frame(self, frame):
        for off in range(0, len(frame), 8):
            self.send_chunk(CAN_TX, frame[off:off + 8])

    def recv(self, timeout=0.5):
        self.s.settimeout(timeout)
        try:
            cf, _ = self.s.recvfrom(16)
        except socket.timeout:
            return None, None
        cid, dlc = struct.unpack("<IB", cf[:5])
        return cid & 0x1FFFFFFF, cf[8:8 + dlc]


# ------------------------------------------------------------------ 解码
def describe(fr):
    mid = fr["msg_id"]
    name = NAMES.get(mid, "unknown")
    p = fr["payload"]
    extra = ""
    if mid == MSG_IMU_PACK and len(p) >= 26:
        g = struct.unpack_from("<3f", p, 14)
        extra = "  gyro/acc=(%.4f, %.4f, %.4f)" % g
    elif mid == MSG_BOTTOM_PACK and len(p) >= 44:
        u0 = struct.unpack_from("<H", p, 21)[0]
        u1 = struct.unpack_from("<H", p, 23)[0]
        extra = "  f21=%d f23=%d f25=%d f31=%d" % (
            u0, u1, p[25], struct.unpack_from("<H", p, 29)[0])
    return "0x%04X %-18s len=%2d %s%s" % (
        mid, name, len(p), hx(p[:24]), extra)


# ------------------------------------------------------------------ 命令
def cmd_listen(can, seconds, quiet):
    print("监听 can0 (Ctrl-C 退出) ...")
    buf = bytearray()
    end = None if seconds <= 0 else time.time() + seconds
    n = 0
    while end is None or time.time() < end:
        cid, data = can.recv(0.5)
        if data is None:
            continue
        if cid != CAN_RX:
            continue
        if data and data[0] == 0x55:
            if buf:
                fr = parse_frame(bytes(buf))
                if fr and not quiet:
                    print(describe(fr))
                    n += 1
            buf = bytearray(data)
        elif buf:
            buf += data
    print("共解析 %d 个报文" % n)


def cmd_send(can, msg_id, payload, repeat, interval, label):
    seq = int(time.time()) & 0xFFFF
    for i in range(repeat):
        frame = build_frame(msg_id, payload, seq=(seq + i) & 0xFFFF)
        can.send_frame(frame)
        if i == 0:
            print("%s: msg=0x%04X payload=%s frame(%dB)=%s"
                  % (label, msg_id, hx(payload) or "(空)", len(frame), hx(frame)))
        if repeat > 1:
            time.sleep(interval)
    if repeat > 1:
        print("已发送 %d 次（间隔 %.2fs）" % (repeat, interval))


def main():
    ap = argparse.ArgumentParser(description="T40 雷达 ESC 的 DUML over CAN 工具")
    ap.add_argument("--iface", default="can0")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("listen", help="只听并解码")
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("speed", help="设置转速 (rpm)")
    p.add_argument("rpm", type=int)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.2)

    for name in ("stop", "enable", "disable"):
        p = sub.add_parser(name)
        p.add_argument("--repeat", type=int, default=1)
        p.add_argument("--interval", type=float, default=0.2)

    p = sub.add_parser("hms", help="HMS 开关")
    p.add_argument("value", type=int, choices=(0, 1))
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.2)

    p = sub.add_parser("query", help="查询 ESC 内部状态 (0x1EFE)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.2)

    p = sub.add_parser("raw", help="自定义发送")
    p.add_argument("cmd_set", help="十六进制，如 1E")
    p.add_argument("cmd_id", help="十六进制，如 80")
    p.add_argument("payload", nargs="?", default="")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.2)

    a = ap.parse_args()
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 2
    can = Can(a.iface)

    if a.cmd == "listen":
        cmd_listen(can, a.seconds, a.quiet)
    elif a.cmd == "speed":
        if not 0 <= a.rpm <= 1800:
            print("rpm 范围 0-1800（固件上限 1800，上电默认 60）", file=sys.stderr)
            return 2
        cmd_send(can, MSG_MOTOR_SPEED, struct.pack("<H", a.rpm),
                 a.repeat, a.interval, "motor_speed=%drpm" % a.rpm)
    elif a.cmd == "stop":
        cmd_send(can, MSG_MOTOR_STOP, b"", a.repeat, a.interval, "motor_stop")
    elif a.cmd == "enable":
        cmd_send(can, MSG_MOTOR_ENABLE, b"", a.repeat, a.interval, "motor_enable")
    elif a.cmd == "disable":
        cmd_send(can, MSG_MOTOR_DISABLE, b"", a.repeat, a.interval, "motor_disable")
    elif a.cmd == "hms":
        cmd_send(can, MSG_HMS_SWITCH, bytes([a.value]),
                 a.repeat, a.interval, "hms_switch=%d" % a.value)
    elif a.cmd == "query":
        cmd_send(can, MSG_STATE_QUERY, b"", a.repeat, a.interval, "state_query")
    elif a.cmd == "raw":
        mid = (int(a.cmd_set, 16) << 8) | int(a.cmd_id, 16)
        payload = bytes.fromhex(a.payload.replace(" ", "")) if a.payload else b""
        cmd_send(can, mid, payload, a.repeat, a.interval, "raw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
