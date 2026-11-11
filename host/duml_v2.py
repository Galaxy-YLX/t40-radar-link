#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
duml_v2.py —— DUML v2 帧解析 + 雷达感知数据（0x1E61 perception_v2）解码

DUML v2 帧结构（字段位置来自 leommxj/dji-agras-radar 的 radar_min_client.py，
并用实测报文验证过）:

    0     1   magic        0x55
    1     1   (b & 0x3F) 必须 == 0x18
    2     1   (b & 0xFC) 必须 == 0x08
    3     2   total_len    u16 LE（等于整帧长度）
    5     2   ?
    7     4   seq          u32 LE
    11    1   ?
    12    2   sender       u16 LE
    14    4   ?
    18    2   receiver     u16 LE
    20    3   ?
    23    1   header CRC8  覆盖 [0:23]
    24    2   msg_id       (cmd_set << 8) | cmd_id
    26    1   ?
    27    N   payload      = data[27:-2]
    -2    2   CRC16        覆盖 [:-2]

0x1E61 perception_v2 payload:

    0   65   头部（含义未知，subtype 在偏移 65）
    65   1   subtype      0=ground_points 1=wire_candidates 2=pcla_obstacle_candidates
    66   3   ?
    69   N×16 条目，每条 <HfffH> = id(u16), x(f32), y(f32), z(f32), metric(u16)

用法:
    python3 duml_v2.py --file cap.bin          # 解析抓包文件（每行一个十六进制包）
    python3 duml_v2.py --udp 40204 --seconds 10
"""
import argparse
import struct
import sys

CRC8_INIT = 0x77
CRC16_INIT = 0x3692
V2_HDR_CRC_OFF = 23
V2_MSG_OFF = 24
V2_PAYLOAD_OFF = 27
V2_MIN_LEN = 29

PERCEPTION_HDR_LEN = 69
PERCEPTION_ENTRY_LEN = 16
PERCEPTION_SUBTYPE_OFF = 65

SUBTYPE_NAMES = {
    0: "ground_points",
    1: "wire_candidates",
    2: "pcla_obstacle_candidates",
}

MSG_NAMES = {
    0x1E61: "perception_v2",
    0x1E62: "avoidance_result_fc",
    0x1E32: "avoidance_result_alt",
    0x1E0B: "radar_imu_pack",
    0x1E0F: "motor_bottom_pack",
    0x0032: "activation",
    0x1EFD: "data_transfer",
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


def parse_v2(data):
    """返回 dict 或 None（校验失败/不是 v2 帧）。"""
    if len(data) < V2_MIN_LEN or data[0] != 0x55:
        return None
    if (data[1] & 0x3F) != 0x18 or (data[2] & 0xFC) != 0x08:
        return None
    total = struct.unpack_from("<H", data, 3)[0]
    if total != len(data):
        return None
    if crc8(data[:V2_HDR_CRC_OFF]) != data[V2_HDR_CRC_OFF]:
        return None
    if struct.unpack_from("<H", data, total - 2)[0] != crc16(data[:-2]):
        return None
    return {
        "total_len": total,
        "seq": struct.unpack_from("<I", data, 7)[0],
        "sender": struct.unpack_from("<H", data, 12)[0],
        "receiver": struct.unpack_from("<H", data, 18)[0],
        "msg_id": (data[V2_MSG_OFF] << 8) | data[V2_MSG_OFF + 1],
        "payload": data[V2_PAYLOAD_OFF:-2],
    }


def decode_perception(payload):
    """0x1E61 payload -> (subtype, [ {id,x,y,z,metric}, ... ])"""
    if len(payload) < PERCEPTION_HDR_LEN:
        raise ValueError("payload too short: %d" % len(payload))
    body_len = len(payload) - PERCEPTION_HDR_LEN
    if body_len % PERCEPTION_ENTRY_LEN:
        raise ValueError("bad body length: %d" % body_len)
    subtype = payload[PERCEPTION_SUBTYPE_OFF]
    count = body_len // PERCEPTION_ENTRY_LEN
    out = []
    for i in range(count):
        off = PERCEPTION_HDR_LEN + i * PERCEPTION_ENTRY_LEN
        item_id, x, y, z, metric = struct.unpack_from("<HfffH", payload, off)
        out.append({"id": item_id, "x": x, "y": y, "z": z, "metric": metric})
    return subtype, out


def summarize(frames):
    """对一批解析结果做统计，便于验证。"""
    import collections
    cnt = collections.Counter()
    subs = collections.Counter()
    bad_lens = collections.Counter()
    pts = []
    for fr in frames:
        cnt[fr["msg_id"]] += 1
        if fr["msg_id"] == 0x1E61:
            try:
                st, entries = decode_perception(fr["payload"])
            except ValueError:
                subs["bad"] += 1
                bad_lens[len(fr["payload"])] += 1
                continue
            subs[st] += 1
            pts.extend(entries)
    lines = []
    lines.append("报文类型 / 帧长分布:")
    size_by_msg = collections.defaultdict(collections.Counter)
    for fr in frames:
        size_by_msg[fr["msg_id"]][fr["total_len"]] += 1
    for mid, n in cnt.most_common():
        sizes = size_by_msg[mid].most_common(6)
        lines.append("  0x%04X %-22s %4d 帧  帧长: %s"
                     % (mid, MSG_NAMES.get(mid, "?"), n,
                        ", ".join("%dB×%d" % (s, c) for s, c in sizes)))
    lines.append("0x1E61 subtype 分布: %s" % dict(subs))
    if bad_lens:
        lines.append("0x1E61 长度异常: %s" % dict(bad_lens))
    if pts:
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        zs = [p["z"] for p in pts]
        ms = [p["metric"] for p in pts]
        lines.append("条目总数 %d" % len(pts))
        lines.append("  x: %.2f .. %.2f m" % (min(xs), max(xs)))
        lines.append("  y: %.2f .. %.2f m" % (min(ys), max(ys)))
        lines.append("  z: %.2f .. %.2f m" % (min(zs), max(zs)))
        lines.append("  metric: %d .. %d" % (min(ms), max(ms)))
        lines.append("前 5 条:")
        for p in pts[:5]:
            lines.append("    id=%-5d x=%7.2f y=%7.2f z=%7.2f metric=%d"
                         % (p["id"], p["x"], p["y"], p["z"], p["metric"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="抓包文件：每行一个十六进制 UDP 负载")
    ap.add_argument("--udp", type=int, help="直接监听 UDP 端口")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--dump-msg", help="十六进制 msg_id，打印其前几个 payload 的 hexdump")
    ap.add_argument("--dump-n", type=int, default=3)
    a = ap.parse_args()

    frames = []
    if a.file:
        for line in open(a.file):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fr = parse_v2(bytes.fromhex(line))
            if fr:
                frames.append(fr)
    elif a.udp:
        import socket
        import time
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", a.udp))
        s.settimeout(0.5)
        t0 = time.time()
        while time.time() - t0 < a.seconds:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            fr = parse_v2(data)
            if fr:
                frames.append(fr)
    else:
        ap.print_help()
        return 1

    print("解析成功 %d 帧" % len(frames))
    print(summarize(frames))
    if a.dump_msg:
        want = int(a.dump_msg, 16)
        shown = 0
        for fr in frames:
            if fr["msg_id"] != want:
                continue
            p = fr["payload"]
            print("\n=== msg 0x%04X  payload %d 字节 ===" % (want, len(p)))
            for off in range(0, len(p), 16):
                chunk = p[off:off + 16]
                print("  %04x: %s" % (off, " ".join("%02x" % b for b in chunk)))
            shown += 1
            if shown >= a.dump_n:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
