#!/bin/sh
# 开机自启：雷达网卡(AR1021x)掉线自动重连
# 真正的活由 /root/radar_usb_watch.sh 干，这里只负责把它拉起来（幂等）。
# 注意本文件名故意不叫 radar_usb_watch.sh —— 否则 pgrep -f 会匹配到自己的
# 命令行，脚本会以为"已经在跑了"从而永远不起。
if pgrep -f '[r]adar_usb_watch\.sh' >/dev/null 2>&1; then
    exit 0
fi
setsid nohup sh /root/radar_usb_watch.sh > /dev/null 2>&1 < /dev/null &
