#!/bin/sh
# ---------------------------------------------------------------------------
# radar_bringup.sh —— 雷达侧后台看护（由 start_all.sh 拉起，也可单独跑）
#
#   等 AR1021x 网卡出现 → 起 radar_wifi AP (192.168.8.1) → 等雷达关联
#   → 起 radar_min.py（flight_pos + CAN 桥接）
#
# 设计原则：**只许它等别人，不许别人等它**。雷达网卡没枚举、AP 起不来、
# 雷达不关联，都只写日志，绝不阻塞 start_all.sh 的电脑链路。
# ---------------------------------------------------------------------------
set -u

IF_RADAR=${IF_RADAR:-wlx00037f2044d2}
AP_RADAR=${AP_RADAR:-/root/ap_1021.conf}
HOSTAPD=${HOSTAPD:-/usr/sbin/hostapd}
MODE=${MODE:-2}
RPM=${RPM:-1200}
RADAR_WAIT=${RADAR_WAIT:-90}

say() { echo "[$(date '+%H:%M:%S')] $*"; }

say "看护启动，等 $IF_RADAR 最多 ${RADAR_WAIT}s"

i=0
while [ ! -e "/sys/class/net/$IF_RADAR" ] && [ $i -lt "$RADAR_WAIT" ]; do
    i=$((i+1)); sleep 1
done

if [ ! -e "/sys/class/net/$IF_RADAR" ]; then
    say "!! ${RADAR_WAIT}s 等不到 $IF_RADAR —— 雷达 USB 没枚举（检查 0cf3:1022 是否在 lsusb / 线材 / hub 口）"
    exit 1
fi

say "雷达网卡出现: $IF_RADAR"
iw reg set US 2>/dev/null
ip link set "$IF_RADAR" up
ip addr flush dev "$IF_RADAR" 2>/dev/null
ip addr add 192.168.8.1/24 dev "$IF_RADAR" 2>/dev/null

if ! pgrep -f "hostapd.*$AP_RADAR" >/dev/null 2>&1; then
    say "启动雷达 AP ($IF_RADAR)"
    "$HOSTAPD" -B "$AP_RADAR" > /root/ap_radar.log 2>&1
    sleep 4
fi

if pgrep -f "hostapd.*$AP_RADAR" >/dev/null 2>&1; then
    say "雷达 AP 就绪: $(iw dev "$IF_RADAR" info 2>/dev/null | awk '/ssid/{print $2}')"
else
    say "!! 雷达 AP 启动失败，看 /root/ap_radar.log"
    exit 1
fi

# 等雷达真的关联上（AP 模式下用 rx_packets 增量判断）
i=0
while [ $i -lt 60 ]; do
    r1=$(cat "/sys/class/net/$IF_RADAR/statistics/rx_packets" 2>/dev/null || echo 0)
    sleep 1
    r2=$(cat "/sys/class/net/$IF_RADAR/statistics/rx_packets" 2>/dev/null || echo 0)
    [ "$r2" -gt "$r1" ] && { say "雷达已连上"; break; }
    i=$((i+1))
done
[ $i -ge 60 ] && say "!! 60 秒没等到雷达数据（radar_min 照样启动）"

if ! pgrep -f "python3.*radar_min\.py" >/dev/null 2>&1; then
    say "启动 radar_min.py (mode=$MODE rpm=$RPM)"
    setsid nohup python3 -u /root/radar_min.py --mode "$MODE" --rpm "$RPM" \
          --flight-ctrl --no-speed-relay > /root/radarmin.log 2>&1 < /dev/null &
    sleep 3
fi

pgrep -f "python3.*radar_min\.py" >/dev/null 2>&1 && say "radar_min: 运行中" || say "!! radar_min 没起来，看 /root/radarmin.log"
say "看护结束"
