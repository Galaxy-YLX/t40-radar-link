#!/bin/sh
# ---------------------------------------------------------------------------
# radar_usb_watch.sh —— 雷达网卡（AR1021x, 0cf3:1022）掉线后自动重连
#
# 背景（2026-11-11 实测）：这颗 AR1021x 是经一块转接板接到板子的 USB hub 上的，
# 接触/供电边缘时会：
#   * 开机根本没枚举上（/sys/bus/usb/devices 里连 1-1.2 都没有）；
#   * 或者枚举上了但 probe 失败：`usb 1-1.2: device descriptor read/8, error -71`
#     / `can't set config #1, error -71` / ath6kl `Failed to init ath6kl core: -71`。
# 这种状态下重启软件没用，只能让 USB 端口重新枚举一次。本脚本就干这件事。
#
# 做的事（从轻到重，全部只碰 USB sysfs，**绝不 rmmod**）：
#   1. 设备在、模块没加载     → insmod ath6kl_core.ko / ath6kl_usb.ko
#   2. 模块加载了、网口没出来 → 对 1-1.2 做一次 authorized 0/1（重新枚举该设备）
#   3. 设备根本不在总线上     → 对那颗 Genesys hub (05e3:0610) 做 unbind/bind，
#                              逼内核重新扫一遍它的端口
#   4. 网口出来了             → 起 radar_bringup.sh（hostapd + radar_min）
#
# 绝对不碰 MT7612U（在 usb2 上，是电脑那条链路），所以重连过程不会断 SSH。
#
# 日志：/root/radar_usb_watch.log     暂停：touch /root/radar_usb_watch.skip
# ---------------------------------------------------------------------------
set -u

IF_RADAR=${IF_RADAR:-wlx00037f2044d2}
HUB_VIDPID=${HUB_VIDPID:-05e3:0610}
DEV_VID=${DEV_VID:-0cf3}
DEV_PID=${DEV_PID:-1022}
INTERVAL=${INTERVAL:-12}
SKIP=${SKIP:-/root/radar_usb_watch.skip}
LOG=${LOG:-/root/radar_usb_watch.log}
BRINGUP=${BRINGUP:-/root/radar_bringup.sh}

say() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 同一条消息 60 s 内只记一次
LAST=""; LAST_TS=0
say1() {
    now=$(date +%s)
    if [ "$1" != "$LAST" ] || [ $((now - LAST_TS)) -ge 60 ]; then
        LAST="$1"; LAST_TS=$now; say "$1"
    fi
}

# 找 0cf3:1022 在哪个 usb 设备节点上，找不到输出空
dev_node() {
    timeout 6 sh -c '
        for d in /sys/bus/usb/devices/*/idVendor; do
            [ "$(cat "$d" 2>/dev/null)" = "'"$DEV_VID"'" ] || continue
            p=$(cat "$(dirname "$d")/idProduct" 2>/dev/null)
            [ "$p" = "'"$DEV_PID"'" ] && { basename "$(dirname "$d")"; exit 0; }
        done
        exit 1' 2>/dev/null
}

# 找 05e3:0610 那颗 hub 的节点名（可能多个）
hub_nodes() {
    timeout 6 sh -c '
        for d in /sys/bus/usb/devices/*/idVendor; do
            [ "$(cat "$d" 2>/dev/null)" = "05e3" ] || continue
            p=$(cat "$(dirname "$d")/idProduct" 2>/dev/null)
            [ "$p" = "0610" ] && basename "$(dirname "$d")"
        done' 2>/dev/null
}

mods_loaded() { grep -q '^ath6kl_usb ' /proc/modules 2>/dev/null; }

re_enum_device() {   # $1 = 设备节点名，如 1-1.2
    say "  重新枚举设备 $1"
    echo 0 > "/sys/bus/usb/devices/$1/authorized" 2>/dev/null
    sleep 2
    echo 1 > "/sys/bus/usb/devices/$1/authorized" 2>/dev/null
    sleep 3
}

re_enum_hub() {      # 逼 hub 重新扫端口
    nodes=$(hub_nodes)
    [ -z "$nodes" ] && { say1 "找不到 $HUB_VIDPID hub，跳过重扫"; return; }
    for n in $nodes; do
        say "  重扫 hub $n"
        echo "$n" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null
        sleep 2
        echo "$n" > /sys/bus/usb/drivers/usb/bind 2>/dev/null
        sleep 3
    done
}

load_mods() {
    say "  insmod ath6kl"
    insmod /root/ath6kl_core.ko 2>/dev/null
    insmod /root/ath6kl_usb.ko 2>/dev/null
    sleep 6
}

ensure_ap() {
    if pgrep -f 'hostapd.*ap_1021' >/dev/null 2>&1; then
        return 0
    fi
    say "  网口在但雷达 AP 没跑，拉起 radar_bringup.sh"
    IF_RADAR="$IF_RADAR" RADAR_WAIT=60 setsid nohup sh "$BRINGUP" \
        >> /root/radar_bringup.log 2>&1 < /dev/null &
    sleep 2
}

say "启动：等 $DEV_VID:$DEV_PID 出现在 USB 上，每 ${INTERVAL}s 看一次"

absent=0
while :; do
    if [ -e "$SKIP" ]; then
        sleep "$INTERVAL"; continue
    fi

    dev=$(dev_node)

    if [ -n "$dev" ]; then
        absent=0
        if [ -e "/sys/class/net/$IF_RADAR" ]; then
            say1 "雷达网口 $IF_RADAR 正常（$dev）"
            ensure_ap
        else
            if ! mods_loaded; then
                say "设备 $dev 在，加载 ath6kl 模块"
                load_mods
            else
                # 模块在但网口没出来：多半是 probe 失败后设备卡住
                re_enum_device "$dev"
                if ! mods_loaded; then load_mods; fi
            fi
            if [ -e "/sys/class/net/$IF_RADAR" ]; then
                say "  ✔ 网口出来了：$IF_RADAR"
                ensure_ap
            else
                say1 "设备 $dev 在但网口还没出来"
            fi
        fi
    else
        absent=$((absent + 1))
        # 设备完全不在总线上：逼 hub 重扫。每轮都做，但日志 60 s 一条
        say1 "USB 上没有 $DEV_VID:$DEV_PID（第 ${absent} 次），重扫 hub"
        if [ -e "/sys/class/net/$IF_RADAR" ]; then
            :   # 网口还在但设备没了？等它自己掉，不动
        fi
        re_enum_hub
    fi

    sleep "$INTERVAL"
done
