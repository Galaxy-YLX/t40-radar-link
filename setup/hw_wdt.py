#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hw_wdt.py —— 用 Zynq 内部看门狗（cadence-wdt）做「用户态卡死就硬复位」。

为什么需要它：2026-11-10/11 两次实测，ath6kl 楔死之后
  * sshd 连得上但 20 s 不给 banner；
  * 桥接的 STATUS/HEARTBEAT 一帧没有（采集线程卡在 iw/子进程里永不返回）；
  * rx 停了、雷达不出点、上位机只能干等；
  * `reboot` 都不一定干净（D 状态进程杀不掉）。
这种状态下**只有断电能救**。板卡上本来就有两颗 cadence 看门狗
（fd4d0000 60 s / ff150000 10 s），之前一直没人接管，
所以这里用 fd4d0000 那颗：喂狗的条件不是"定时喂"，而是
「桥接的状态采集最近 N 秒成功过」。

判据用桥接写的心跳文件 /tmp/t40_bridge_stat.ts 的 mtime —— 它由
t40_bridge.py 的采集线程独立维护（和有没有上位机连着无关）。
采集线程卡死 → 文件不动 → 这里不再喂狗 → 看门狗超时 → SoC 硬复位。

⚠ 2026-11-11 实测：**板卡上这两颗 cadence SWDT 超时了也不拉复位**
（停喂 65 s，uptime 一路涨；SWDT0 的 DT 里有 reset-on-timeout 也不管用）。
所以现在硬件看门狗只是"第一层"，真正兜底的是第二层软件重启：
判死后 --soft-after 秒还没复位就 `reboot`，再 25 s 没动静就 sysrq-b。
软的那层救不了"整机内核死锁"，但救得了我们实际遇到的"ath6kl 楔死、
其他进程还活着"——这正是它存在的意义。要拿到真正的硬件复位，
得去查 ZynqMP 的 SWDT 复位路由（FSBL/CRL_APB），那是下一步的事。

安全阀：
  * 开机 GRACE 秒内无条件喂（等 start_all / 桥接起来）；
  * /root/hw_wdt.skip 存在 → 无条件喂（人工动板子时别让它重启）；
  * 桥接**没在监听** → 认为人是故意停的，只记日志不判死；
  * 30 分钟内判死 3 次 → 彻底退出保护（写 /root/hw_wdt.disarmed），
    防止"某个配置错误"造成无限重启风暴；
  * 判死期间如果采集自己恢复了，立刻恢复喂狗，不白重启。

用法：
    python3 hw_wdt.py --dry-run          # 只看判断，不碰 /dev/watchdog
    python3 hw_wdt.py --dev /dev/watchdog0 --timeout 30 --grace 240
    touch /root/hw_wdt.skip              # 临时不判死（人工动板子时）
    rm -f /root/hw_wdt.disarmed          # 判死 3 次被停用后，修好了再打开

注意：本进程**退出即等于解除保护**（内核 NOWAYOUT 没开，关 fd 就停表）。
所以外部要用 while 循环把它拉起来，见 setup/40-hw-wdt.sh。
"""
import argparse
import errno
import fcntl
import os
import signal
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------- ioctl 常量
# linux/watchdog.h
WATCHDOG_IOCTL_BASE = ord('W')


def _ioc(direction, nr, size=4):
    return (direction << 30) | (size << 16) | (WATCHDOG_IOCTL_BASE << 8) | nr


WDIOC_KEEPALIVE = _ioc(2, 5)      # _IOR
WDIOC_SETTIMEOUT = _ioc(3, 6)     # _IOWR
WDIOC_GETTIMEOUT = _ioc(2, 7)     # _IOR

DEFAULT_DEV = "/dev/watchdog0"
DEFAULT_HEALTH = "/tmp/t40_bridge_stat.ts"
DEFAULT_SKIP = "/root/hw_wdt.skip"


def uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def file_age(path):
    """文件多久没被写过（秒）。不存在返回 None。"""
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return None


def ioctl_int(fd, req, value=None):
    """带 int 参数的 watchdog ioctl；value=None 表示只读。"""
    buf = bytearray(struct.pack("i", 0 if value is None else int(value)))
    fcntl.ioctl(fd, req, buf, True)
    return struct.unpack("i", bytes(buf))[0]


def bridge_listening(port=46000):
    """桥接有没有在监听（查 /proc/net/tcp，不依赖 pgrep / ss）。"""
    want = "%04X" % port
    try:
        with open("/proc/net/tcp") as f:
            f.readline()                     # 表头
            for line in f:
                p = line.split()
                if len(p) < 4:
                    continue
                if p[1].split(":")[-1] == want and p[3] == "0A":   # 0A = LISTEN
                    return True
    except Exception:
        return False
    return False


class HwWdt(object):
    def __init__(self, a):
        self.a = a
        self.fd = None
        self.timeout = a.timeout
        self.period = max(2.0, a.timeout / 3.0)
        self.feeding = True          # 当前是否在喂狗
        self.given_up = False        # 已判死、正在等复位
        self.last_key = ""
        self.last_note_ts = 0.0
        self.t0 = time.time()
        self.reboots_path = a.reboots

    # ------------------------------------------------------------ 日志
    def note(self, text, force=False, key=None):
        """同一条状态最多 60 s 记一次（日志写 SD 卡，别刷爆）。

        key 用来区分"状态"和"文案"：健康时的文案里带着心跳年龄，每次都不同，
        直接比文案等于每次都记一遍。
        """
        now = time.time()
        key = key if key is not None else text
        if not force and key == self.last_key and now - self.last_note_ts < 60:
            return
        self.last_key = key
        self.last_note_ts = now
        line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), text)
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(self.a.log, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------ 设备
    def open_wdt(self):
        for path in [self.a.dev, "/dev/watchdog"]:
            if not path or not os.path.exists(path):
                continue
            try:
                fd = os.open(path, os.O_WRONLY)
            except OSError as exc:
                self.note("打开 %s 失败: %s" % (path, exc), force=True)
                continue
            self.fd = fd
            self.dev = path
            # 把超时设短一点：默认 60 s 对"卡死检测"太慢
            try:
                real = ioctl_int(fd, WDIOC_SETTIMEOUT, self.a.timeout)
                self.timeout = real or self.a.timeout
                self.note("接管 %s，超时 %d s（请求 %d s）"
                          % (path, self.timeout, self.a.timeout), force=True)
            except Exception as exc:
                try:
                    self.timeout = ioctl_int(fd, WDIOC_GETTIMEOUT)
                except Exception:
                    pass
                self.note("接管 %s，SETTIMEOUT 不支持(%s)，沿用驱动默认 %d s"
                          % (path, exc, self.timeout), force=True)
            self.period = max(2.0, self.timeout / 3.0)
            return True
        self.note("没有可用的 /dev/watchdog*（本次不提供硬复位保护）", force=True)
        return False

    def keepalive(self):
        if self.fd is None:
            return
        try:
            ioctl_int(self.fd, WDIOC_KEEPALIVE, 0)
        except Exception as exc:
            self.note("KEEPALIVE 失败: %s" % exc, force=True)

    def disarm(self, why):
        """显式停表：写 'V'（magic close）后关闭。NOWAYOUT 未开才有效。"""
        if self.fd is not None:
            try:
                os.write(self.fd, b"V")
            except Exception:
                pass
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        self.note("已解除硬件看门狗：%s" % why, force=True)

    # ------------------------------------------------------------ 判据
    def health(self):
        """返回 (是否健康, 说明)。

        只认一种病：**心跳文件本来在涨，忽然不涨了**（采集线程卡死）。
        其余情况一律放行：

          * 桥接没在监听 → 多半是人为停掉或在改代码；
          * 心跳文件从来没出现过 → 刚开机、或者采集从启动就卡住。
            后者听起来也该救，但实测会造成"开机 5 分钟一重启"的死循环
            （每次开机 ath6kl 都可能楔死），比不救更糟。这种"开机就没起来"
            交给 radar_watchdog 的规则去管，它有 30 分钟 3 次的上限。
        """
        if os.path.exists(self.a.skip):
            return True, "存在 %s，跳过判死" % self.a.skip
        up = uptime()
        if up < self.a.grace:
            return True, "开机 %.0f s，宽限期内（%d s）" % (up, self.a.grace)
        if not bridge_listening(self.a.port):
            return True, "桥接没在监听 :%d（人为停掉/还没起），不判死" % self.a.port
        age = file_age(self.a.health)
        if age is None:
            return True, "心跳文件还没出现过（刚开机？），不判死"
        if age <= self.a.stale:
            return True, "采集正常（心跳 %.1f s 前）" % age
        return False, "采集已 %.0f s 没更新（心跳文件 %s）" % (age, self.a.health)

    def recent_giveups(self):
        """最近 30 分钟内判死了几次。

        板卡没有 RTC，每次开机时钟都退回到 2021-08-31，写在文件里的旧时间戳
        会变成"未来时间"。所以必须把 t > now 的条目也算作过期丢掉，
        否则几次历史记录会永远算作"刚刚发生"，重启风暴的闸门直接失效。
        """
        now = time.time()
        n = 0
        for t in self._read_giveups():
            if 0 <= (now - t) < 1800:
                n += 1
        return n

    def _read_giveups(self):
        out = []
        try:
            with open(self.reboots_path) as f:
                for line in f:
                    try:
                        out.append(int(line.strip()))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def record_giveup(self):
        now = int(time.time())
        keep = [t for t in self._read_giveups() if 0 <= (now - t) < 1800]
        keep.append(now)
        try:
            with open(self.reboots_path, "w") as f:
                for t in keep[-20:]:
                    f.write("%d\n" % t)
        except Exception:
            pass

    # ------------------------------------------------------------ 主循环
    def run(self):
        if not self.open_wdt():
            return 2
        unhealthy_since = 0.0
        soft_done = False
        sysrq_done = False
        while True:
            healthy, why = self.health()
            now = time.time()
            if healthy:
                if self.given_up:
                    self.note("采集中途恢复，继续喂狗（没有重启板卡）", force=True)
                    self.given_up = False
                self.feeding = True
                unhealthy_since = 0.0
                soft_done = False
                sysrq_done = False
                self.keepalive()
                self.note(why, key="healthy")
            else:
                if not self.given_up:
                    n = self.recent_giveups()
                    if n >= self.a.max_giveups:
                        self.record_giveup()
                        self.disarm("30 分钟内已判死 %d 次，退出保护等人工" % n)
                        try:
                            open(self.a.disarmed, "w").write(
                                "disarmed at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            pass
                        return 3
                    self.record_giveup()
                    self.given_up = True
                    self.feeding = False
                    unhealthy_since = now
                    self.note("停止喂狗：%s → 约 %d s 后硬复位"
                              % (why, self.timeout), force=True)
                else:
                    self.note("仍在等复位：%s" % why, key="unhealthy")
                    # 板卡上这两颗 cadence SWDT 实测「超时了也不拉复位」
                    # （2026-11-11：停喂 65 s，uptime 一路涨，SWDT0/SWDT1 都没动）。
                    # 所以再加一层软件兜底：先正常 reboot，再不生效就 sysrq-b。
                    t_un = now - unhealthy_since if unhealthy_since else 0.0
                    if self.a.reboot_fallback:
                        if not soft_done and t_un >= self.a.soft_after:
                            soft_done = True
                            self.note("硬件看门狗 %d s 没动作，改发起 reboot"
                                      % self.timeout, force=True)
                            self.soft_reboot()
                        elif soft_done and not sysrq_done and t_un >= self.a.soft_after + 25:
                            sysrq_done = True
                            self.note("reboot 也没生效，直接 sysrq-b", force=True)
                            self.sysrq_boot()
                # 不喂狗，保持进程活着（退出会解除保护）
            time.sleep(self.period)
        return 0

    def soft_reboot(self):
        for cmd in (["/sbin/reboot"], ["/bin/reboot"], ["reboot"]):
            try:
                subprocess.Popen(cmd, stdout=open(os.devnull, "wb"),
                                 stderr=subprocess.STDOUT,
                                 close_fds=True, start_new_session=True)
                return True
            except Exception:
                continue
        self.note("reboot 拉不起来", force=True)
        return False

    def sysrq_boot(self):
        try:
            fd = os.open("/proc/sysrq-trigger", os.O_WRONLY)
            try:
                os.write(fd, b"b")
            finally:
                os.close(fd)
            return True
        except Exception as exc:
            self.note("sysrq-b 失败: %s" % exc, force=True)
            return False


def main():
    ap = argparse.ArgumentParser(description="Zynq 硬件看门狗（按桥接采集健康度喂狗）")
    ap.add_argument("--dev", default=DEFAULT_DEV)
    ap.add_argument("--timeout", type=int, default=30, help="看门狗超时秒数（尽量设小）")
    ap.add_argument("--grace", type=int, default=240, help="开机宽限秒数")
    ap.add_argument("--stale", type=float, default=25.0, help="心跳多久没更新算卡死")
    ap.add_argument("--health", default=DEFAULT_HEALTH)
    ap.add_argument("--skip", default=DEFAULT_SKIP)
    ap.add_argument("--port", type=int, default=46000, help="桥接监听端口（判桥接起没起）")
    ap.add_argument("--soft-after", type=int, default=40,
                    help="判死后多少秒硬件还没动作就改走软件 reboot")
    ap.add_argument("--reboot-fallback", dest="reboot_fallback", action="store_true",
                    default=True, help="硬件看门狗没生效时用 reboot/sysrq-b 兜底（默认开）")
    ap.add_argument("--no-reboot-fallback", dest="reboot_fallback", action="store_false",
                    help="只依赖硬件看门狗，不做软件重启")
    ap.add_argument("--log", default="/root/hw_wdt.log")
    ap.add_argument("--reboots", default="/root/hw_wdt.giveups")
    ap.add_argument("--disarmed", default="/root/hw_wdt.disarmed")
    ap.add_argument("--max-giveups", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="只打印判断，不碰设备")
    a = ap.parse_args()

    w = HwWdt(a)
    if a.dry_run:
        healthy, why = w.health()
        print("健康: %s\n原因: %s" % (healthy, why))
        print("心跳文件: %s（age=%s）" % (a.health, file_age(a.health)))
        print("uptime: %.0f s  宽限: %d s  判死阈值: %.0f s"
              % (uptime(), a.grace, a.stale))
        print("最近 30 分钟判死次数: %d" % w.recent_giveups())
        print("桥接是否在监听 :%d → %s" % (a.port, bridge_listening(a.port)))
        for dev in (a.dev, "/dev/watchdog"):
            if dev and os.path.exists(dev):
                st = os.stat(dev)
                print("%s 存在，rdev=%d:%d" % (dev, os.major(st.st_rdev), os.minor(st.st_rdev)))
        return 0

    def on_signal(signum, frame):
        w.disarm("收到信号 %d" % signum)
        sys.exit(0)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, on_signal)
        except Exception:
            pass
    try:
        return w.run()
    finally:
        if w.fd is not None:
            w.disarm("进程退出")


if __name__ == "__main__":
    sys.exit(main())
