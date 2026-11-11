#!/bin/sh
# ---------------------------------------------------------------------------
# 40-hw-wdt.sh —— 硬件看门狗（Zynq cadence-wdt）的守护外壳
#
# 为什么要有外壳：hw_wdt.py 一旦退出，内核就把看门狗表停了（NOWAYOUT 没开），
# 保护等于没开。所以这里用 while 循环把它拉起来；只有它自己判"该退出保护"
# （rc=3，或存在 /root/hw_wdt.disarmed）时才停。
#
# 两个入口都会拉起它，靠下面的 ps 判断做幂等：
#   1. /etc/systemd/system/t40-hwwdt.service   ← 开机就起，不依赖 start_all
#   2. /root/autostart/40-hw-wdt.sh           ← start_all 扫描时也会起一次
# 之所以要两条：最常见的那种楔死就发生在 start_all 自己（insmod ath6kl）里，
# 那时 autostart 根本轮不到执行，只能靠 systemd 那条。
#
# 手动暂停判死：touch /root/hw_wdt.skip
# 彻底停用：    touch /root/hw_wdt.disarmed   （rm 掉后重新开机会自动恢复）
# 日志：        /root/hw_wdt.log
# ---------------------------------------------------------------------------
set -u

MARK=/root/hw_wdt.disarmed
LOG=/root/hw_wdt.log

say() { echo "[$(date '+%m-%d %H:%M:%S')] [wrapper] $*" >> "$LOG"; }

# 上一份如果还在（systemctl restart 时 KillMode 只杀了外壳），先送走它。
# 不能像以前那样"看到有人在跑就退出"：那样 restart 永远换不上新代码。
# 注意本脚本自己的 cmdline 里没有 hw_wdt.py，所以这里不会误杀自己。
stale=$(ps -eo pid,args 2>/dev/null | awk '/[h]w_wdt\.py/ {print $1}')
if [ -n "$stale" ]; then
    say "清理残留 hw_wdt.py: $stale"
    for p in $stale; do kill -9 "$p" 2>/dev/null; done
    sleep 1
fi

if [ -e "$MARK" ]; then
    say "存在 $MARK，硬件看门狗不启用（rm 掉它可恢复）"
    exit 0
fi

say "启动 hw_wdt.py"
while :; do
    python3 -u /root/hw_wdt.py
    rc=$?
    say "hw_wdt.py 退出 rc=$rc"
    [ "$rc" = "3" ] && { say "它自己决定退出保护，不再拉起"; exit 0; }
    [ -e "$MARK" ] && { say "存在 $MARK，不再拉起"; exit 0; }
    sleep 2
done
