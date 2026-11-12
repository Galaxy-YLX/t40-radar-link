#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_testdata.py —— 从真实 pcap 里抽若干帧 0x00F0，生成安卓端单元测试用的数据。

安卓端的解码是 Python 解码器的手工移植，最容易出的错是偏移写错一位、少乘/多乘 1000。
所以这里不重新实现一遍解码，而是**直接 import host/t40_cloud_view.py 里的解码器**
算出期望值，再用 Kotlin 单元测试对拍 —— 两边不一致就说明移植错了。

用法：
    python3 android/tools/make_testdata.py [pcap] [帧数]

默认输入 captures/collect_run1/06_pcap0.pcap（开发仓里的采集包；开源包不含采集数据，
这时要自己指定 pcap），输出到 android/app/src/test/resources/{frames.bin,expected.txt}。

frames.bin 格式：重复 [u32 LE 长度][UDP 负载]
expected.txt 每行：n sumOk(0/1) seq az0 el0 r0 c30 c50 rpm
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))          # radar/
HOST = os.path.join(ROOT, "host")
OUT = os.path.join(HERE, "..", "app", "src", "test", "resources")

sys.path.insert(0, HOST)
import t40_cloud_view as cv                                       # noqa: E402


def main():
    pcap = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "captures", "collect_run1", "06_pcap0.pcap")
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    if not os.path.exists(pcap):
        raise SystemExit(
            "找不到 pcap: %s\n"
            "这个脚本要拿**你自己的**雷达抓包来生成期望值（开源包里不含采集数据）。\n"
            "用法: python3 %s <你的 pcap> [帧数]" % (pcap, sys.argv[0]))

    frames = cv.PcapSource._read(pcap)
    pkts = cv.PcapSource._udp_40204(frames)
    print("pcap: %s  以太网帧 %d  UDP:40204 包 %d" % (pcap, len(frames), len(pkts)))

    os.makedirs(OUT, exist_ok=True)
    bin_path = os.path.join(OUT, "frames.bin")
    exp_path = os.path.join(OUT, "expected.txt")

    got = 0
    with open(bin_path, "wb") as fb, open(exp_path, "w", encoding="utf-8") as fe:
        fe.write("# n sumOk seq az0 el0 r0 c30 c50 rpm\n")
        for _ts, payload in pkts:
            d = cv.decode_00f0(payload)
            if d is None or d["n"] == 0:
                continue
            rpm = cv.rpm_from_c5(d["az"], d["c5"]) or 0.0
            fb.write(struct.pack("<I", len(payload)))
            fb.write(payload)
            fe.write("%d %d %d %.6f %.6f %.6f %.1f %.1f %.6f\n" % (
                d["n"], 1 if d["sum_ok"] else 0, d["seq"],
                d["az"][0], d["el"][0], d["r"][0], d["c3"][0], d["c5"][0], rpm))
            got += 1
            if got >= want:
                break

    if got == 0:
        raise SystemExit("没抽到 0x00F0 帧，换个 pcap 试试")

    print("写出 %d 帧 → %s (%d 字节)" % (got, bin_path, os.path.getsize(bin_path)))
    print("期望值 → %s" % exp_path)
    ns = [int(l.split()[0]) for l in open(exp_path, encoding="utf-8")
          if not l.startswith("#")]
    print("每帧点数 min=%d max=%d 平均=%.1f 总点数=%d"
          % (min(ns), max(ns), sum(ns) / len(ns), sum(ns)))


if __name__ == "__main__":
    main()
