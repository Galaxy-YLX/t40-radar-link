#!/bin/sh
# 开机自启入口：拉起雷达链路看门狗（/root/radar_watchdog.sh）
# 由 /root/start_all.sh 扫描 /root/autostart/*.sh 执行
#
# 2026-11-11 起看门狗改由 systemd 托管（t40-radarwd.service，Restart=always）：
# 之前这样 nohup 起的进程跑着跑着会无声消失（8-31 19:58 之后再没日志），
# 而"看门狗自己没了"比"看门狗误判"更难发现。
# 这里保留入口只为兼容没有安装 unit 的板卡，装了就不再重复拉起 ——
# 否则两个实例会互相 pkill（老的入口开头就 pkill -f radar_watchdog）。
if systemctl -q is-enabled t40-radarwd.service 2>/dev/null; then
    echo "t40-radarwd.service 已启用，autostart 不再重复拉起"
    exit 0
fi
exec sh /root/radar_watchdog.sh
