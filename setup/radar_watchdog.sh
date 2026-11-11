#!/bin/sh
# ---------------------------------------------------------------------------
# radar_watchdog.sh —— 雷达链路看门狗（2026-11-10 起，2026-11-11 加自愈）
#
# 背景：板卡跑着跑着会「假死」——ath6kl Oops 之后 iw / sysfs 调用永不返回，
# 桥接的 STATUS 采集被钉死、radar 40204 再没有一个包、sshd 连上不给 banner。
# 这种状态软件救不回来，但**可以用软件触发重启**，不必再靠人工断电。
#
# 规则（满足任一条就重启板卡）：
#   A. dmesg 里有 ath6kl 的 Oops / 固件下载失败                        ← 楔死的铁证
#   B. /tmp/t40_bridge_stat.ts 超过 STAT_STALE 秒没更新                ← 状态采集卡住
#   C. 雷达 AP 网卡开机 IF_WAIT 秒后还不存在，**且 USB 上确实有 0cf3:1022**
#      （有设备没网卡 = 驱动的问题；设备都不在总线上 = 雷达没上电，重启没用）
#   D. 雷达 AP 口 rx 连续 NO_PKT 秒不涨，且有关联的 station，软恢复无效
#   E. 本次开机从没见过包，但 station 在，且已过 NEVER_FLOW_WAIT 秒
#
# 不重启的情况：雷达 AP 口没有 station（雷达没上电/被拔）只记日志；
#              30 分钟内已经重启过 3 次 → 停下等人工（防重启风暴）。
#
# 保护自己不会挂住：所有可能卡在 D 状态的命令（iw）都后台跑 + 轮询 + kill -9，
# 绝不用 $( ) 直接等它；只用 /proc/net/dev 和文件 mtime 做判据。
# 2026-11-11：iw 只在"rx 已经停了 NO_PKT 秒"这一步当**最后一问**（问有没有 station，
# 用来区分"雷达没上电"和"驱动楔死"），不进主循环；USE_IW=0 可彻底关掉它，
# 那时只要 rx 停了就按卡死处理（靠 30 分钟 3 次的重启上限兜底）。
#
# 手动暂停：touch /root/radar_watchdog.skip
# 日志：/root/radar_watchdog.log
# 手动跑：sh /root/radar_watchdog.sh &     停：pkill -f radar_watchdog
# ---------------------------------------------------------------------------
set -u

IF_RADAR=${IF_RADAR:-wlx00037f2044d2}
AP_RADAR=${AP_RADAR:-/root/ap_1021.conf}
GRACE_BOOT=${GRACE_BOOT:-300}     # 开机后多久才开始管
NO_PKT=${NO_PKT:-60}              # 多少秒没涨包算异常
RECOVER=${RECOVER:-25}            # 软恢复后等多久
IF_WAIT=${IF_WAIT:-600}           # 开机多久还没网卡就重启
NEVER_FLOW_WAIT=${NEVER_FLOW_WAIT:-900}   # 一直没数据多久后也重启
STAT_TS=${STAT_TS:-/tmp/t40_bridge_stat.ts}
STAT_STALE=${STAT_STALE:-180}     # 桥接状态采集多久没更新算卡死
USE_IW=${USE_IW:-1}               # 0 = 完全不碰 iw，只用 /proc/net/dev
SKIP=${SKIP:-/root/radar_watchdog.skip}
LOG=${LOG:-/root/radar_watchdog.log}
REBOOTS=${REBOOTS:-/root/radar_watchdog.reboots}

say() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

LAST_MSG=""
LAST_MSG_TS=0
# 同样的消息最多 60 s 记一次：规则每 10 s 判一次，不能刷爆日志
say_throttled() {
    now=$(date +%s)
    if [ "$1" != "$LAST_MSG" ] || [ $((now - LAST_MSG_TS)) -ge 60 ]; then
        LAST_MSG="$1"
        LAST_MSG_TS=$now
        say "$1"
    fi
}

rx_bytes() {
    awk -v i="$1:" '$1==i {print $2}' /proc/net/dev 2>/dev/null | head -1
}

# 输出 "站数|状态"，状态 ok / hung。iw 卡在 D 状态时 kill -9 也杀不掉，
# 所以这里只等到 5 秒就放弃，**不 wait 它**，免得看门狗自己挂住。
stations() {
    tmp=/tmp/wd_station.$$
    iw dev "$1" station dump > "$tmp" 2>/dev/null &
    pid=$!
    i=0
    while [ "$i" -lt 5 ]; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
        i=$((i + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        echo "0|hung"
        rm -f "$tmp" 2>/dev/null
        return
    fi
    wait "$pid" 2>/dev/null
    n=$(grep -c '^Station' "$tmp" 2>/dev/null)
    [ -z "$n" ] && n=0
    rm -f "$tmp" 2>/dev/null
    echo "$n|ok"
}

ath6kl_oops() {
    dmesg 2>/dev/null | grep -E 'ath6kl' \
        | grep -qE 'bad PC value|Failed to write firmware|Failed to submit usb control message|Oops'
}

# 雷达的 AR1021x 在不在 USB 总线上（0cf3:1022）。
# 整个循环套 timeout：USB 子系统卡死时读 sysfs 会挂住，宁可当成"不在"。
usb_present() {
    timeout 6 sh -c '
        for d in /sys/bus/usb/devices/*/idVendor; do
            [ "$(cat "$d" 2>/dev/null)" = "'"$1"'" ] || continue
            p=$(cat "$(dirname "$d")/idProduct" 2>/dev/null)
            [ "$p" = "'"$2"'" ] && exit 0
        done
        exit 1' 2>/dev/null
}

bridge_running() {
    # 46000（0xB3D0）在监听就算桥接活着。查 /proc/net/tcp，不依赖 pgrep ——
    # busybox 的 pgrep 行为不一，而且在容器/别的环境里会误判。
    awk -v p="B3D0" 'NR > 1 {
            split($2, a, ":")
            if (a[2] == p && $4 == "0A") { found = 1; exit }
        }
        END { exit(found ? 0 : 1) }' /proc/net/tcp 2>/dev/null
}

stat_age() {
    # 桥接状态文件和 /tmp 的 mtime 差了多少秒；文件不存在返回 99999
    [ -f "$STAT_TS" ] || { echo 99999; return; }
    now=$(date +%s)
    mt=$(date -r "$STAT_TS" +%s 2>/dev/null)
    [ -z "$mt" ] && { echo 99999; return; }
    echo $((now - mt))
}

recent_reboots() {
    [ -f "$REBOOTS" ] || { echo 0; return; }
    now=$(date +%s)
    cnt=0
    while read -r t; do
        [ -n "$t" ] || continue
        [ $((now - t)) -lt 1800 ] && cnt=$((cnt + 1))
    done < "$REBOOTS"
    echo "$cnt"
}

do_reboot() {
    if [ -e "$SKIP" ]; then
        say "  (存在 $SKIP，跳过重启)"
        return 1
    fi
    n=$(recent_reboots)
    if [ "$n" -ge 3 ]; then
        say "  → 30 分钟内已重启 $n 次，不再自动重启，等人工处理"
        return 1
    fi
    say "  → 重启板卡（30 分钟内第 $((n + 1)) 次）"
    date +%s >> "$REBOOTS"
    tail -n 20 "$REBOOTS" > "$REBOOTS.tmp" 2>/dev/null && mv "$REBOOTS.tmp" "$REBOOTS"
    sync
    reboot
    sleep 60
    return 0
}

say "看门狗启动：if=$IF_RADAR NO_PKT=${NO_PKT}s STAT_STALE=${STAT_STALE}s GRACE_BOOT=${GRACE_BOOT}s"

last_rx=""
last_change=$(date +%s)
seen_data=0
warned_never=0

while :; do
    sleep 10

    up=$(cut -d. -f1 /proc/uptime)
    if [ "$up" -lt "$GRACE_BOOT" ]; then
        last_change=$(date +%s)      # 开机宽限期，不计时
        last_rx=""
        continue
    fi

    # ---- A. ath6kl Oops：楔死的铁证，直接重启 ----
    if ath6kl_oops; then
        say_throttled "dmesg 里有 ath6kl Oops/固件失败（uptime ${up}s）"
        do_reboot
        last_change=$(date +%s)
        continue
    fi

    # ---- B. 桥接状态采集卡住：STATUS 还在发，但内容是旧的 ----
    #      只有"桥接进程在跑、但状态不再更新"才归它；桥接没跑是另一回事，别乱重启。
    sa=$(stat_age)
    if [ "$sa" -gt "$STAT_STALE" ]; then
        if bridge_running; then
            say_throttled "桥接状态 ${sa}s 没更新（采集卡住，uptime ${up}s）"
            do_reboot
            last_change=$(date +%s)
        else
            say_throttled "t40_bridge 没在跑（状态文件 $STAT_TS 陈旧 ${sa}s），只记日志不重启"
        fi
        continue
    fi

    # ---- C. 雷达 AP 网卡不存在 ----
    #      要区分两件事：USB 上有没有那颗 AR1021x。
    #      有设备没网卡 = 驱动/USB 的问题，重启板卡有意义；
    #      设备根本不在总线上 = 雷达没上电 / 线松了 / hub 没枚举，重启板卡纯属白折腾
    #      （2026-11-11 实测：雷达 AR1021x 掉出 USB 总线后，这条规则每 10 分钟重启一次板卡，
    #        重启 3 次仍然没有设备，最后还得人工去看线）。
    if [ ! -e "/sys/class/net/$IF_RADAR" ]; then
        if usb_present 0cf3 1022; then
            if [ "$up" -gt "$IF_WAIT" ]; then
                say_throttled "USB 上有 0cf3:1022 但没有网卡 $IF_RADAR（uptime ${up}s）→ 重启板卡"
                do_reboot
            fi
        else
            say_throttled "雷达 USB(0cf3:1022) 不在总线上，网卡 $IF_RADAR 当然没有 —— 查雷达供电/线材，不重启"
        fi
        continue
    fi

    now=$(date +%s)
    rx=$(rx_bytes "$IF_RADAR")
    [ -z "$rx" ] && rx=0

    if [ -z "$last_rx" ]; then      # 第一次采样只记账，不算"出过包"
        last_rx="$rx"
        last_change=$now
        continue
    fi

    if [ "$rx" != "$last_rx" ]; then
        last_rx="$rx"
        last_change=$now
        if [ "$seen_data" -eq 0 ]; then
            seen_data=1
            say "雷达开始出包（rx=$rx）"
        fi
        continue
    fi

    idle=$((now - last_change))
    [ "$idle" -lt "$NO_PKT" ] && continue

    if [ "$USE_IW" = "0" ]; then
        # 完全不碰 iw：默认"雷达就在那儿"，直接走软恢复 → 重启那条路。
        # 代价是雷达真被拔掉时会白重启，由 do_reboot 的 30 分钟 3 次上限兜住。
        st=1
        sflag="skip"
    else
        res=$(stations "$IF_RADAR")
        st=${res%%|*}
        sflag=${res##*|}
        [ -z "$st" ] && st=0
    fi

    if [ "$sflag" = "hung" ]; then
        # iw 自己卡住 → 驱动/内核已经楔死，不再等"有没有 station"
        say_throttled "读 station 的 iw 卡死（rx ${idle}s 没涨）→ 按驱动楔死处理"
        do_reboot
        last_change=$now
        continue
    fi

    say_throttled "雷达口 ${idle}s 没涨包（rx=$rx station=$st seen_data=$seen_data）"

    if [ "$st" -eq 0 ]; then
        say_throttled "  → 没有 station：雷达不在线（没上电/被拔），不重启，继续观察"
        last_change=$now
        continue
    fi

    if [ "$seen_data" -eq 0 ]; then
        # 这次开机从来没来过包：可能是刚上电还没就绪，给足时间再动手
        if [ $((idle / 600)) -gt "$warned_never" ]; then
            warned_never=$((idle / 600))
            say "  → 本次开机还没收到过雷达数据（已等 ${idle}s）"
        fi
        if [ "$idle" -gt "$NEVER_FLOW_WAIT" ]; then
            say_throttled "  → 超过 ${NEVER_FLOW_WAIT}s 一直没有数据，按卡死处理"
            do_reboot
            last_change=$now
        fi
        continue
    fi

    say_throttled "  → 有 station 但没数据：先重启雷达 AP 软恢复"
    pkill -f "hostapd.*ap_1021" 2>/dev/null
    sleep 2
    ip link set "$IF_RADAR" up 2>/dev/null
    ip addr flush dev "$IF_RADAR" 2>/dev/null
    ip addr add 192.168.8.1/24 dev "$IF_RADAR" 2>/dev/null
    timeout 15 hostapd -B "$AP_RADAR" >/dev/null 2>&1
    sleep "$RECOVER"

    rx2=$(rx_bytes "$IF_RADAR")
    [ -z "$rx2" ] && rx2=0
    if [ "$rx2" != "$rx" ]; then
        say "  → 软恢复成功（rx $rx → $rx2）"
        last_rx="$rx2"
        last_change=$(date +%s)
        continue
    fi

    say "  → 软恢复无效"
    do_reboot
    last_change=$(date +%s)
done
