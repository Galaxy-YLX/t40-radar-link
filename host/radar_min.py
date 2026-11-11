#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radar_min.py —— 最小雷达客户端（板卡侧）

做三件事：
  1. 以 10 Hz 往 192.168.8.2:50007 发 0x1E21 flight_pos（mode=2），打开 1E61 门控
  2. 把雷达发到 50007 的电机 DUML（0x1E80/0x1E82/0x1E83/0x1EF9…）转发到 CAN 给 ESC
  3. 把 ESC 回在 CAN 上的 DUML 转发回雷达（hms_switch 的 ACK 靠这条路）

没有第 2、3 步，雷达的 motor FSM 停在初始态，1E61 永远是 69 字节空包。

用法:
    python3 radar_min.py                 # 默认 can0 + 50007
    python3 radar_min.py --rpm 900       # 顺便主动设转速
"""
import argparse
import socket
import struct
import sys
import threading
import time

# ------------------------------------------------------------------ 常量
CRC8_INIT = 0x77
CRC16_INIT = 0x3692

CAN_TX_ID = 0x0A2     # 主机 -> ESC
CAN_RX_ID = 0x0A3     # ESC -> 主机
DUML_SENDER_ESC = 0x78
DUML_RECEIVER_ESC = 0x18

# 端点编码 (type<<5)|id。实测雷达自己发的是 sender=0x78 receiver=0xC3：
#   0x78 = 雷达 app.elf（type 3, id 0x18）
#   0xC3 = FC（type 6, id 3）
# 所以作为 FC 发过去应该是 sender=0xC3 / receiver=0x78。
UDP_SENDER = 0xC3
UDP_RECEIVER = 0x78

# 需要桥接到 CAN 的电机相关 cmd_id
MOTOR_CMD_IDS = {0x09, 0x0A, 0x0F, 0x80, 0x81, 0x82, 0x83, 0xF9, 0xFE}

NAMES = {
    0x1E09: "motor_enable", 0x1E0A: "motor_disable", 0x1E0B: "radar_imu",
    0x1E0F: "bottom_pack", 0x1E21: "flight_pos", 0x1E22: "flight_ctrl",
    0x1E32: "avoid_alt", 0x1E61: "perception_v2", 0x1E62: "avoid_fc",
    0x1E80: "motor_speed", 0x1E82: "motor_stop", 0x1E83: "hms_switch",
    0x1EF9: "motor_state_f9", 0x1EFE: "motor_state_query", 0x00F0: "blackbox_v2",
}


def crc8(data, init=CRC8_INIT):
    c = init & 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8C) & 0xFF if (c & 1) else (c >> 1) & 0xFF
    return c


def crc16(data, init=CRC16_INIT):
    c = init & 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c >> 1) ^ 0x8408) & 0xFFFF if (c & 1) else (c >> 1) & 0xFFFF
    return c


def build_v1(msg_id, payload, seq, sender, receiver, flags=0):
    total = 11 + len(payload) + 2
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


def parse_v1(d):
    if len(d) < 13 or d[0] != 0x55:
        return None
    total = d[1] | ((d[2] & 0x03) << 8)
    if total != len(d):
        return None
    return {
        "sender": d[4], "receiver": d[5],
        "seq": struct.unpack_from("<H", d, 6)[0],
        "flags": d[8],
        "msg_id": (d[9] << 8) | d[10],
        "payload": d[11:-2],
    }


def build_flight_pos(mode=2):
    p = bytearray(66)
    struct.pack_into("<4f", p, 0, 1.0, 0.0, 0.0, 0.0)
    struct.pack_into("<6h", p, 16, 0, 0, 0, 0, 0, 0)
    struct.pack_into("<3h", p, 28, 0, 0, 0)
    p[38] = mode & 0xFF
    struct.pack_into("<2d", p, 40, 0.0, 0.0)
    p[56] = 0
    struct.pack_into("<d", p, 57, 0.0)
    p[65] = 0
    return bytes(p)


class Client(object):
    def __init__(self, args):
        self.args = args
        self.seq = 0
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", args.port))
        self.udp.settimeout(0.2)
        self.can = None
        self.can_lock = threading.Lock()
        self.stat = {"can_rx": 0, "can_tx": 0, "udp_rx": 0, "udp_tx": 0}

    # -------------------------------------------------- 发送
    def udp_send(self, msg_id, payload, sender=UDP_SENDER, receiver=UDP_RECEIVER):
        self.seq = (self.seq + 1) & 0xFFFF
        frame = build_v1(msg_id, payload, self.seq, sender, receiver)
        # 2026-11-10：这里原来没兜底，一次 socket.timeout 就把整个进程带走。
        # 崩了以后没人周期性转 ESC 的 0x1E80，ESC 超时停转 → 雷达不转 → 0x00F0 全没了。
        try:
            self.udp.sendto(frame, (self.args.radar, self.args.port))
            self.stat["udp_tx"] += 1
        except Exception as exc:
            n = self.stat.get("udp_tx_err", 0) + 1
            self.stat["udp_tx_err"] = n
            if n <= 3 or n % 200 == 0:
                print("UDP 发送失败（累计 %d 次）：%s" % (n, exc))

    def can_send(self, msg_id, payload):
        if self.can is None:
            return
        frame = build_v1(msg_id, payload, self.seq, DUML_SENDER_ESC, DUML_RECEIVER_ESC)
        with self.can_lock:
            for off in range(0, len(frame), 8):
                chunk = frame[off:off + 8]
                self.can.send(struct.pack("<IB3x8s", CAN_TX_ID, len(chunk),
                                          chunk.ljust(8, b"\0")))
        self.stat["can_tx"] += 1

    # -------------------------------------------------- 线程
    def flight_pos_loop(self):
        """按固定频率喂 0x1E21 flight_pos。

        雷达的 update_height_fusion_from_flight_pos 是「收到一包更新一次」，
        10Hz 时它对飞行模式/位置的响应就锁在 100ms 粒度上；50Hz 是飞控常规
        速率，雷达侧不会因此增加带宽压力（66 字节 × 50 = 3.3 KB/s）。
        """
        pos = build_flight_pos(self.args.mode)
        period = 1.0 / max(1.0, float(self.args.hz))
        next_tx = time.time()
        while True:
            try:
                now = time.time()
                if now >= next_tx:
                    next_tx += period
                    if next_tx < now:      # 落后太多就别追了，直接对齐到当前
                        next_tx = now + period
                    self.udp_send(0x1E21, pos)
                time.sleep(max(0.0, next_tx - time.time()))
            except Exception as exc:
                print("flight_pos_loop 异常，继续跑：%s" % exc)
                time.sleep(0.2)

    def flight_ctrl_loop(self):
        """前 10 秒每秒补发 0x1E22 flight_ctrl，触发 hms_switch 打开电机运行门。"""
        t0 = time.time()
        while time.time() - t0 < 10.0:
            ctrl = bytearray(7)
            ctrl[0] = 1        # sub_id 1
            ctrl[1] = 1        # !=0 才开
            ctrl[2] = 0
            ctrl[3] = 1        # motor_state
            self.udp_send(0x1E22, bytes(ctrl))
            time.sleep(1.0)

    def udp_loop(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                continue
            fr = parse_v1(data)
            if not fr:
                continue
            self.stat["udp_rx"] += 1
            mid = fr["msg_id"]
            if (mid >> 8) == 0x1E and (mid & 0xFF) in MOTOR_CMD_IDS:
                # 雷达进入工作态后会持续下发自己的目标转速（0x1E80），
                # 桥接过去就会把手动设的转速覆盖掉。--no-speed-relay 时丢弃它。
                if self.args.no_speed_relay and mid == 0x1E80:
                    if self.args.verbose:
                        print("UDP<- 0x1E80 已忽略（--no-speed-relay）")
                    continue
                self.can_send(mid, fr["payload"])
                if self.args.verbose:
                    print("UDP->CAN 0x%04X %-16s %s"
                          % (mid, NAMES.get(mid, "?"), fr["payload"].hex()))
            elif self.args.verbose:
                print("UDP<- 0x%04X %-16s len=%d"
                      % (mid, NAMES.get(mid, "?"), len(fr["payload"])))

    def can_loop(self):
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            s.bind((self.args.can,))
            s.settimeout(0.5)
            self.can = s
        except Exception as exc:
            print("CAN 打开失败: %s" % exc)
            return
        print("CAN 就绪: %s" % self.args.can)
        buf = bytearray()
        while True:
            try:
                cf, _ = s.recvfrom(16)
            except socket.timeout:
                continue
            except Exception:
                continue
            cid, dlc = struct.unpack("<IB", cf[:5])
            cid &= 0x1FFFFFFF
            if cid != CAN_RX_ID:
                continue
            self.stat["can_rx"] += 1
            data = cf[8:8 + dlc]
            if data and data[0] == 0x55:
                if len(buf) >= 13:
                    fr = parse_v1(bytes(buf))
                    if fr:
                        self.udp_send(fr["msg_id"], fr["payload"],
                                      fr["sender"], fr["receiver"])
                buf = bytearray(data)
            elif buf:
                buf += data
                if len(buf) >= 3:
                    total = buf[1] | ((buf[2] & 3) << 8)
                    if total and len(buf) >= total:
                        fr = parse_v1(bytes(buf[:total]))
                        if fr:
                            self.udp_send(fr["msg_id"], fr["payload"],
                                          fr["sender"], fr["receiver"])
                        buf = bytearray(buf[total:])

    def status_loop(self):
        while True:
            time.sleep(5)
            print("[stat] udp_rx=%d udp_tx=%d(err %d) can_rx=%d can_tx=%d"
                  % (self.stat["udp_rx"], self.stat["udp_tx"],
                     self.stat.get("udp_tx_err", 0),
                     self.stat["can_rx"], self.stat["can_tx"]))

    def run(self):
        threading.Thread(target=self.can_loop, daemon=True).start()
        threading.Thread(target=self.udp_loop, daemon=True).start()
        threading.Thread(target=self.status_loop, daemon=True).start()
        if self.args.flight_ctrl:
            threading.Thread(target=self.flight_ctrl_loop, daemon=True).start()
        time.sleep(1.0)
        if self.args.rpm is not None:
            self.can_send(0x1E80, struct.pack("<H", self.args.rpm))
            print("已下发转速 %d rpm" % self.args.rpm)
        while True:                    # 万一还是抛出来，重启这个循环，别让整个程序退出
            try:
                self.flight_pos_loop()
            except Exception as exc:
                print("flight_pos_loop 退出，1 s 后重启：%s" % exc)
                time.sleep(1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radar", default="192.168.8.2")
    ap.add_argument("--port", type=int, default=50007)
    ap.add_argument("--can", default="can0")
    ap.add_argument("--mode", type=int, default=2)
    ap.add_argument("--hz", type=float, default=50.0,
                    help="0x1E21 flight_pos 发送频率（默认 50Hz，原来是 10Hz）")
    ap.add_argument("--rpm", type=int, default=None)
    ap.add_argument("--flight-ctrl", action="store_true",
                    help="前 10 秒补发 0x1E22 flight_ctrl 开电机运行门")
    ap.add_argument("--no-speed-relay", action="store_true",
                    help="不转发雷达自己的 0x1E80 转速指令，让手动设的转速生效")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    print("radar_min: radar=%s:%d can=%s mode=%d hz=%.0f"
          % (a.radar, a.port, a.can, a.mode, a.hz))
    Client(a).run()


if __name__ == "__main__":
    main()
