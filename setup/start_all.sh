#!/bin/sh
# ---------------------------------------------------------------------------
# start_all.sh —— 板卡上电后一键恢复整套链路（v4, 2026-11-10：ath6kl 延迟探测 + 重试）
#
#   电脑 MT7612U (USB) → mt76x2u → hostapd t40_link (2.4G ch6, 192.168.9.1) + dnsmasq
#   雷达 AR1021x (USB) → ath6kl → hostapd radar_wifi (5G ch48, 192.168.8.1)
#   雷达 app.elf (UDP 50007) ←→ radar_min.py ←→ CAN ←→ ESC
#   雷达感知数据 (UDP 40204) → t40_bridge.py → TCP 46000 → 电脑上位机
#
# v3 相对 v2 的三处修正（都是冷启动实测踩出来的）：
#   1. USB 枚举是异步的：dwc3 加载后 hub/网卡要几十秒才出现，早一步探测就会
#      漏掉 MT7612U 的 ZeroCD 切模式 → 现在会轮询等待，等不到就重绑 xhci 再试。
#   2. ath6kl 的 insmod 放后台，卡在 D 状态也不会把脚本顶死（v2 已改）。
#   3. 脚本**必须 exit 0**：以前最后一行 `running && say` 在 AP 没起来时返回 1，
#      systemd 判 failed 后会把 cgroup 里的 radar_bringup / bridge 一起清掉。
#
# 用法:
#   sh /root/start_all.sh              # 起全部
#   sh /root/start_all.sh --no-link    # 不起电脑链路（t40_link + dnsmasq）
#   sh /root/start_all.sh --no-radar   # 不起雷达侧（ath6kl + 雷达 AP + radar_min）
#   sh /root/start_all.sh --no-bridge  # 不起 t40_bridge
#   sh /root/start_all.sh --stop       # 全停
#   RPM=60 sh /root/start_all.sh       # 改转速
# ---------------------------------------------------------------------------
set -u

IF_RADAR=wlx00037f2044d2      # 雷达自己的 AR1021x
IF_LINK=wlx24050fb42703       # MT7612U，给电脑用
AP_RADAR=/root/ap_1021.conf
AP_LINK=/root/hostapd_t40link.conf
HOSTAPD=/usr/sbin/hostapd
RPM=${RPM:-1200}
MODE=${MODE:-2}
RADAR_WAIT=${RADAR_WAIT:-240}
USB_WAIT=${USB_WAIT:-45}

say() { echo "[$(date '+%H:%M:%S')] $*"; }
running() { pgrep -f "$1" >/dev/null 2>&1; }

# 在 /sys 里找 USB 设备的 idVendor:idProduct，不依赖 lsusb
# （USB 子系统被卡住时 lsusb 自己会挂住不返回）
usb_present() {
    for f in /sys/bus/usb/devices/*/idVendor; do
        d=$(dirname "$f")
        [ "$(cat "$f" 2>/dev/null)" = "$1" ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = "$2" ] && return 0
    done
    return 1
}

# 等 MT7612U 出现（可能是 ZeroCD 的 2870，也可能已经切好的 7612）
wait_mt7612() {
    i=0
    while [ $i -lt "$1" ]; do
        usb_present 0e8d 2870 && return 0
        usb_present 0e8d 7612 && return 0
        i=$((i+1)); sleep 1
    done
    return 1
}

# 重绑 xhci 控制器，强制重新做一次端口检测
# （对应 dmesg 里的 "usb usb1-port1: connect-debounce failed" —— hub 没枚举上）
xhci_rebind() {
    for p in /sys/bus/platform/drivers/xhci-hcd/xhci-hcd.*; do
        [ -d "$p" ] || continue
        n=$(basename "$p")
        say "  重绑 $n"
        echo "$n" > /sys/bus/platform/drivers/xhci-hcd/unbind 2>/dev/null
        sleep 2
        echo "$n" > /sys/bus/platform/drivers/xhci-hcd/bind 2>/dev/null
    done
    sleep 5
}

wait_iface() {  # wait_iface <iface> <秒>
    i=0
    while [ ! -e "/sys/class/net/$1" ] && [ $i -lt "$2" ]; do i=$((i+1)); sleep 1; done
    [ -e "/sys/class/net/$1" ]
}

stop_all() {
    say "停止"
    self=$$
    ppid=$(awk '{print $4}' /proc/$self/stat 2>/dev/null || echo 0)
    # 注意：不能用宽泛的字符串匹配 —— 调用者的命令行里可能正好含这些词，
    # 会把自己/父进程杀掉（曾经踩过）。这里用精确模式 + 排除自身/父进程/PID<=1。
    for pat in 'python3.*radar_min\.py' 'python3.*t40_bridge\.py' '^dnsmasq ' 'radar_bringup\.sh'; do
        for p in $(pgrep -f "$pat" 2>/dev/null); do
            [ "$p" -le 1 ] && continue
            [ "$p" = "$self" ] && continue
            [ "$p" = "$ppid" ] && continue
            kill -9 "$p" 2>/dev/null
        done
    done
    for conf in "$AP_RADAR" "$AP_LINK"; do
        for p in $(pgrep -f "hostapd.*${conf}" 2>/dev/null); do
            [ "$p" -le 1 ] && continue
            [ "$p" = "$self" ] && continue
            [ "$p" = "$ppid" ] && continue
            kill -9 "$p" 2>/dev/null
        done
    done
    say "已停止"
    exit 0
}

[ "${1:-}" = "--stop" ] && stop_all

# ===========================================================================
# A. 电脑链路最先起（这一段结束电脑就该能连上 t40_link）
# ===========================================================================
if [ "${1:-}" != "--no-link" ]; then
    # ---------- 1. USB 主机控制器（dwc3-xilinx 开机不自动加载）----------
    if [ ! -d /sys/bus/usb/devices/usb1 ]; then
        say "加载 dwc3-xilinx"
        modprobe dwc3-xilinx 2>/dev/null
    fi

    # ---------- 2. 等 USB 枚举（关键：这一步以前是竞态）----------
    if ! wait_mt7612 "$USB_WAIT"; then
        say "!! ${USB_WAIT}s 没等到 MT7612U，重绑 xhci 再试一轮"
        xhci_rebind
        wait_mt7612 30 || say "!! 还是没等到 MT7612U（查线材 / hub 供电）"
    fi

    # ---------- 3. MT7612U：ZeroCD 起步，要切模式 ----------
    if usb_present 0e8d 2870; then
        say "MT7612U 切模式 (ZeroCD → 7612)"
        usb_modeswitch -v 0e8d -p 2870 -K >/dev/null 2>&1
        sleep 4
        wait_mt7612 20 || say "!! 切模式后没等到 7612"
    fi
    lsmod | grep -q mt76x2u || { say "加载 mt76x2u"; modprobe mt76x2u 2>/dev/null; sleep 2; }

    if ! wait_iface "$IF_LINK" 20; then
        say "!! 等不到 $IF_LINK，重绑 xhci 最后一试"
        xhci_rebind
        wait_iface "$IF_LINK" 15 || say "!! 仍然等不到 $IF_LINK（MT7612U 没插或没被识别）"
    fi

    # ---------- 4. 电脑链路 AP ----------
    if [ -e "/sys/class/net/$IF_LINK" ]; then
        iw reg set US 2>/dev/null
        ip link set "$IF_LINK" up
        ip addr flush dev "$IF_LINK" 2>/dev/null
        ip addr add 192.168.9.1/24 dev "$IF_LINK" 2>/dev/null
        if ! running "hostapd.*$AP_LINK"; then
            say "启动电脑链路 AP ($IF_LINK)"
            "$HOSTAPD" -B "$AP_LINK" > /root/ap_link.log 2>&1
            sleep 4
        fi
        if running "hostapd.*$AP_LINK"; then
            say "电脑链路 AP 就绪: $(iw dev "$IF_LINK" info 2>/dev/null | awk '/ssid/{print $2}')"
        else
            say "!! 电脑链路 AP 启动失败，看 /root/ap_link.log"
        fi
        if ! pgrep -x dnsmasq >/dev/null 2>&1; then
            say "启动 dnsmasq"
            dnsmasq -i "$IF_LINK" --dhcp-range=192.168.9.10,192.168.9.100,12h \
                    --dhcp-option=3 --dhcp-option=6 --port=0 --bind-interfaces \
                    --pid-file=/run/dnsmasq-t40.pid
        fi
        say "电脑链路: t40_link / 192.168.9.1/24"
    fi
fi

# ---------- 5. CAN ----------
if ! ip -br link show can0 2>/dev/null | grep -q "UP"; then
    say "拉起 can0 (1 Mbps)"
    ip link set can0 type can bitrate 1000000 2>/dev/null
    ip link set can0 up 2>/dev/null
    sleep 1
fi
if ip -br link show can0 2>/dev/null | grep -q "UP"; then
    say "can0: UP"
else
    say "!! can0 没起来"
fi

# ===========================================================================
# B. 雷达侧（全部异步，卡住也不影响上面的电脑链路）
# ===========================================================================
if [ "${1:-}" != "--no-radar" ]; then
    # ---------- 6. ath6kl：延迟探测 + 重试（v4）----------
    # 2026-11-10 实测：开机第 45 秒 insmod 时，AR1021x 的固件下载会超时
    #   ath6kl: Failed to write firmware: -110
    # 之后设备卡在 D 状态、模块还卸不掉，只能断电。判断是 USB 刚枚举完就探的时序问题，
    # 所以这里等总线安静下来（默认 60 s）再探；探不到就隔 15 s 再来，最多 3 次。
    ATH6KL_DELAY=${ATH6KL_DELAY:-45}
    if ! lsmod | grep -q ath6kl_usb; then
        say "ath6kl 延迟 ${ATH6KL_DELAY}s 再探测，最多 6 轮（后台，避开 USB 刚枚举完的坑）"
        (
          sleep "$ATH6KL_DELAY"
          try=1
          while [ $try -le 6 ]; do
            echo "[$(date '+%H:%M:%S')] try $try: insmod"
            insmod /root/ath6kl_core.ko 2>/dev/null
            insmod /root/ath6kl_usb.ko  2>/dev/null
            i=0
            while [ $i -lt 30 ]; do
                if [ -e "/sys/class/net/$IF_RADAR" ]; then
                    echo "[$(date '+%H:%M:%S')] try $try: 接口就绪 $IF_RADAR"
                    # 前面的 radar_bringup 可能已经等超时退出了，这里补一刀（幂等）
                    IF_RADAR="$IF_RADAR" AP_RADAR="$AP_RADAR" MODE="$MODE" RPM="$RPM" \
                        RADAR_WAIT=90 setsid nohup sh /root/radar_bringup.sh \
                        >> /root/radar_bringup.log 2>&1 < /dev/null &
                    exit 0
                fi
                i=$((i+1)); sleep 1
            done
            echo "[$(date '+%H:%M:%S')] try $try: 30s 没出接口"
            # 设备卡死时 rmmod 会失败，那就别硬来了（断电是唯一解）
            if ! timeout 10 rmmod ath6kl_usb 2>/dev/null; then
                echo "[$(date '+%H:%M:%S')] 模块卸不掉（设备卡死），停止重试"
                exit 1
            fi
            timeout 10 rmmod ath6kl_core 2>/dev/null
            try=$((try+1)); sleep 15
          done
          echo "[$(date '+%H:%M:%S')] 六轮都没探到网卡"
        ) </dev/null >/root/ath6kl_load.log 2>&1 &
    fi

    # ---------- 7. 雷达 AP + radar_min：后台看护（等待要覆盖上面的延迟）----------
    RADAR_WAIT=${RADAR_WAIT:-240}
    if ! running "radar_bringup\.sh"; then
        say "启动雷达看护 radar_bringup.sh（后台，等 $IF_RADAR 最多 ${RADAR_WAIT}s）"
        IF_RADAR="$IF_RADAR" AP_RADAR="$AP_RADAR" MODE="$MODE" RPM="$RPM" RADAR_WAIT="$RADAR_WAIT" \
            setsid nohup sh /root/radar_bringup.sh > /root/radar_bringup.log 2>&1 < /dev/null &
    fi
fi

# ---------- 8. bridge ----------
if [ "${1:-}" != "--no-bridge" ]; then
    if ! running "t40_bridge\.py"; then
        say "启动 t40_bridge.py"
        setsid nohup python3 -u /root/t40_bridge.py > /root/bridge.log 2>&1 < /dev/null &
        sleep 2
    fi
fi

# ---------- 9. 用户自定义自启动（/root/autostart/*.sh）----------
if [ -d /root/autostart ]; then
    for f in /root/autostart/*.sh; do
        [ -f "$f" ] || continue
        [ -x "$f" ] || chmod +x "$f" 2>/dev/null
        say "自启动: $f"
        nohup sh "$f" >> /root/autostart.log 2>&1 &
        sleep 1
    done
fi

# ---------- 10. 汇总 ----------
say "完成"
ip -br addr show "$IF_LINK" 2>/dev/null
ip -br addr show "$IF_RADAR" 2>/dev/null
running "radar_min\.py"        && say "  radar_min: 运行中"
running "t40_bridge\.py"       && say "  t40_bridge: 运行中（TCP 46000）"
running "hostapd.*$AP_LINK"    && say "  t40_link AP: 运行中"

# 必须有：最后一条命令失败不能让 systemd 判 failed（会连带清掉后台进程）
exit 0
