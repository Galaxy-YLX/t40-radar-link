#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start_local.sh —— 用本机（PC）代替 XCZU3EG 板卡跑完整 T40 数据链路
#
#   本机 ath6kl 网卡  → radar_wifi AP 192.168.8.1  ← 雷达 STA 192.168.8.2
#   本机 SocketCAN    → can0 (1 Mbps)              ↔ 雷达 ESC (DUML v1)
#   host/radar_min.py → 10~50Hz 0x1E21 门控 + 50007/CAN 电机 DUML 双向转发
#   bridge/t40_bridge.py → UDP 40204 的 0x00F0 点云 → TCP 46000 (T40R)
#   本机上位机        → 127.0.0.1:46000（Radar_2d / t40_cloud_view.py / 安卓）
#
# 与板卡方案的区别：不需要第二块网卡和 t40_link AP、不需要 dnsmasq、
# 不需要板端看门狗/硬件探测；AP 和 CAN 复用仓库根的 setup/start_ap.sh /
# start_can.sh。协议帧格式完全不变，上位机一行都不用改。
#
# 用法:
#   ./start_local.sh [start|stop|restart|status]
#
# 环境变量:
#   BIND (127.0.0.1)   TCP 监听地址；给手机/别的电脑看时设 0.0.0.0
#   PORT (46000)  CAN_IFACE (can0)  RADAR_IP (192.168.8.2)  RPM (1200)
#   WIFI_IFACE (自动探测 192.168.8.1 所在网卡)  LOG_DIR (/tmp/t40-local)
#   SKIP_HW=1     跳过 AP/CAN 配置、不启 FC 门控、桥接加 --no-can（无硬件联调）
#   NO_FC=1       只跳过 FC 门控（已有别的客户端在跑时用）
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK_ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$LINK_ROOT/.." && pwd)"
BRIDGE="$LINK_ROOT/bridge/t40_bridge.py"
FC="$LINK_ROOT/host/radar_min.py"

BIND="${BIND:-127.0.0.1}"
PORT="${PORT:-46000}"
CAN_IFACE="${CAN_IFACE:-can0}"
RADAR_IP="${RADAR_IP:-192.168.8.2}"
RPM="${RPM:-1200}"
LOG_DIR="${LOG_DIR:-/tmp/t40-local}"
SKIP_HW="${SKIP_HW:-0}"
NO_FC="${NO_FC:-0}"

have_ap() { ip -4 addr show 2>/dev/null | grep -q '192\.168\.8\.1'; }
have_can() {
    [ -e "/sys/class/net/$CAN_IFACE" ] &&
    [ "$(cat "/sys/class/net/$CAN_IFACE/operstate" 2>/dev/null)" = "up" ]
}
pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_proc() {  # start_proc <name> <python args...>
    local name="$1"; shift
    local pidf="$LOG_DIR/$name.pid"
    if pid_alive "$pidf"; then
        echo "  $name 已在运行 (pid $(cat "$pidf"))"
        return 0
    fi
    # setsid: 脱离启动脚本的进程组，否则关掉 2D 雷达（或 timeout 结束）时
    # 会把后台桥接/门控一起带走 —— 与板卡 start_all.sh 的做法一致。
    setsid nohup python3 -u "$@" >"$LOG_DIR/$name.log" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$pidf"
    sleep 0.4
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "  $name 启动失败，日志尾部：" >&2
        tail -20 "$LOG_DIR/$name.log" >&2 || true
        rm -f "$pidf"
        return 1
    fi
    echo "  $name 已启动 (pid $pid)"
}

prepare_env() {
    if [ "$SKIP_HW" = "1" ]; then
        echo "[local] SKIP_HW=1：跳过 AP / CAN 配置与 FC 门控（仅联调转发）"
        return 0
    fi
    if have_ap; then
        echo "[local] radar_wifi AP 已就绪 (192.168.8.1)"
    else
        echo "[local] 未找到 192.168.8.1 -> 启动本机雷达 AP"
        if ! sudo "$REPO_ROOT/setup/start_ap.sh"; then
            echo "[local] 警告：AP 启动失败（驱动/网卡没插好？），链路仍会启动" >&2
        fi
    fi
    if have_can; then
        echo "[local] $CAN_IFACE 已 UP"
    else
        echo "[local] $CAN_IFACE 未 UP -> 配置 SocketCAN"
        if ! bash "$REPO_ROOT/setup/start_can.sh"; then
            echo "[local] 警告：CAN 配置失败，上位机电机命令会返回失败" >&2
        fi
    fi
}

wifi_iface() {
    if [ -n "${WIFI_IFACE:-}" ]; then
        echo "$WIFI_IFACE"
        return
    fi
    local found
    found="$(ip -4 -o addr show 2>/dev/null | awk '/192\.168\.8\.1/{print $2; exit}')"
    echo "${found:-wlx00037f2044d2}"
}

do_start() {
    mkdir -p "$LOG_DIR"
    echo "[local] 本机代替板卡启动 T40 链路"
    prepare_env

    local wifi
    wifi="$(wifi_iface)"
    echo "[local] 雷达 AP 网卡: $wifi"

    if [ "$SKIP_HW" != "1" ] && [ "$NO_FC" != "1" ]; then
        start_proc radar_min "$FC" --radar "$RADAR_IP" --can "$CAN_IFACE" \
            --mode 2 --rpm "$RPM" --flight-ctrl --no-speed-relay
    fi

    local args=(--bind "$BIND" --port "$PORT" --can "$CAN_IFACE" \
                --wifi "$wifi" --radar-ip "$RADAR_IP" --name LOCAL)
    if [ "$SKIP_HW" = "1" ]; then
        args+=(--no-can)
    fi
    start_proc bridge "$BRIDGE" "${args[@]}"

    echo
    echo "[local] 链路就绪："
    echo "  TCP   $BIND:$PORT       T40R（0x00F0 点云 / STATUS / CMD）"
    echo "  UDP   :40204 雷达点云   :50007 FC 门控"
    echo "  CAN   $CAN_IFACE"
    echo "  日志  $LOG_DIR/{bridge,radar_min}.log"
    echo
    echo "  2D 雷达：  $REPO_ROOT/Radar_2d/run.sh local"
    echo "  点云 Panels： python3 $LINK_ROOT/host/t40_cloud_view.py --src tcp://127.0.0.1:$PORT"
    echo "  停止链路：  $0 stop"
    echo
    echo "  注意：本机桥接占用了 UDP 40204，2D 雷达请走 link/local 模式，"
    echo "        不要再用 --src udp（会和桥接抢同一个端口）。"
}

do_stop() {
    echo "[local] 停止本机链路（AP/CAN/hostapd 保持不动）"
    for name in bridge radar_min; do
        local pidf="$LOG_DIR/$name.pid"
        if ! pid_alive "$pidf"; then
            rm -f "$pidf"
            echo "  $name 未在运行"
            continue
        fi
        local pid
        pid="$(cat "$pidf")"
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        rm -f "$pidf"
        echo "  $name 已停止 (pid $pid)"
    done
}

do_status() {
    echo "[local] 本机链路状态"
    for name in bridge radar_min; do
        local pidf="$LOG_DIR/$name.pid"
        if pid_alive "$pidf"; then
            echo "  $name: 运行中 (pid $(cat "$pidf"))"
        else
            echo "  $name: 未运行"
        fi
    done
    if python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT"; then
        echo "  TCP 127.0.0.1:$PORT: 可连接"
    else
        echo "  TCP 127.0.0.1:$PORT: 无监听"
    fi
    if have_ap; then echo "  radar_wifi AP: 192.168.8.1 已配置"; else echo "  radar_wifi AP: 未配置"; fi
    if have_can; then echo "  $CAN_IFACE: UP"; else echo "  $CAN_IFACE: DOWN/缺失"; fi
    if [ -f "$LOG_DIR/bridge.log" ]; then
        echo "  bridge.log 尾部："
        tail -5 "$LOG_DIR/bridge.log" | sed 's/^/    /'
    fi
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    *) echo "usage: $0 [start|stop|restart|status]" >&2; exit 1 ;;
esac
