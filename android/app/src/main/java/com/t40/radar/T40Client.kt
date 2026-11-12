package com.t40.radar

import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.LinkedBlockingQueue
import java.util.zip.CRC32

/** 帧头 magic，编码/解析/重同步共用。 */
private val MAGIC = "T40R".toByteArray(Charsets.US_ASCII)

/**
 * 数据源抽象：真机走 TCP，没硬件时走 demo。
 * 界面只认这个接口，所以「连不上板卡也能看界面调样式」。
 */
interface RadarSource {
    val canCmd: Boolean
    val name: String
    fun start()
    fun stop()
    fun setRpm(rpm: Int)
    fun motorEnable()
    fun motorDisable()
    fun stopMotor()
}

/**
 * 板卡转发协议客户端（对应 host/t40_cloud_view.py 的 TcpSource）。
 *
 * 帧格式（全部小端）：
 *
 *     magic(4)="T40R" | ver(1)=1 | type(1) | flags(2) | seq(4) | ts_ns(8) | len(4)
 *     | payload(len) | crc32(4)
 *
 * CRC32/IEEE 覆盖 version..payload（也就是 buf[4, 24+len)）。
 *
 * 线程模型：一个后台线程独占 socket（收 + 发都在里面，避免两个线程写同一 socket）。
 * 回调全在后台线程上发生，界面自己 post 到主线程。
 */
class T40Client(
    private val host: String,
    private val port: Int,
    private val listener: Listener,
) : RadarSource {

    interface Listener {
        /** 链路状态文字变化（连上/断开/重连中）。linkUp 表示现在是否连着。 */
        fun onState(text: String, linkUp: Boolean)
        fun onFrame(f: RadarDecoder.Frame)
        fun onStatus(st: BoardStatus)
        fun onAck(op: Int, result: Int, msg: String)

        /** 板卡日志（桥接的 log() 全走这里）。**不要**拿它当链路状态。 */
        fun onLog(text: String)
    }

    /** 板卡 STATUS（40 字节，`<IIIIIIIBb6sBBH`；旧桥接只有 34/40 字节的差异已在解析里兜住）。 */
    class BoardStatus(
        val uptimeMs: Long,
        val canRx: Long,
        val canTx: Long,
        val canErr: Long,
        val radarPkts: Long,
        val radarBytes: Long,
        val dropped: Long,
        val wifi: Int,
        val rssi: Int,
        val canState: Int,
        val statusAge: Int,
        val atMs: Long,
    )

    override val canCmd: Boolean get() = true
    override val name: String get() = "tcp $host:$port"

    @Volatile private var running = false
    @Volatile private var sock: Socket? = null
    private var thread: Thread? = null
    private val cmds = LinkedBlockingQueue<ByteArray>()

    override fun start() {
        if (running) return
        running = true
        thread = Thread({ runLoop() }, "t40-tcp").apply {
            isDaemon = true
            start()
        }
    }

    override fun stop() {
        running = false
        try {
            sock?.close()
        } catch (_: Exception) {
        }
        sock = null
        thread?.interrupt()
        thread = null
        cmds.clear()
    }

    override fun setRpm(rpm: Int) {
        val r = rpm.coerceIn(0, 65535)
        cmds.offer(byteArrayOf(OP_SET_RPM.toByte(), (r and 0xFF).toByte(), ((r shr 8) and 0xFF).toByte()))
    }

    override fun motorEnable() {
        cmds.offer(byteArrayOf(OP_MOTOR_ENABLE.toByte()))
    }

    override fun motorDisable() {
        cmds.offer(byteArrayOf(OP_MOTOR_DISABLE.toByte()))
    }

    override fun stopMotor() {
        cmds.offer(byteArrayOf(OP_STOP.toByte()))
    }

    // ------------------------------------------------------------------ 主循环
    private fun runLoop() {
        var backoff = 1000L
        while (running) {
            var s: Socket? = null
            try {
                s = Socket()
                s.connect(java.net.InetSocketAddress(host, port), 5000)
                s.tcpNoDelay = true
                s.soTimeout = 200
                sock = s
                backoff = 1000L
                listener.onState("已连上 $host:$port", true)
                serve(s)
                if (running) listener.onState("连接断开，准备重连 …", false)
            } catch (e: Exception) {
                if (running) {
                    val msg = e.message ?: e.javaClass.simpleName
                    listener.onState(
                        "连接 $host:$port 失败：$msg（${backoff / 1000} s 后重试）", false
                    )
                }
            } finally {
                try {
                    s?.close()
                } catch (_: Exception) {
                }
                sock = null
            }
            if (!running) break
            val until = System.currentTimeMillis() + backoff
            while (running && System.currentTimeMillis() < until) {
                try {
                    Thread.sleep(100)
                } catch (_: InterruptedException) {
                    return
                }
            }
            backoff = (backoff * 17 / 10).coerceAtMost(5000L)
        }
    }

    private fun serve(s: Socket) {
        val ins = s.getInputStream()
        val out = s.getOutputStream()
        val rb = StreamBuf(1 shl 19)
        val tmp = ByteArray(65536)
        var lastHb = 0L
        while (running) {
            // 命令：只有这个线程碰 socket
            while (true) {
                val c = cmds.poll() ?: break
                out.write(encode(T_CMD, c))
                out.flush()
            }
            val now = System.currentTimeMillis()
            if (now - lastHb >= 1000L) {
                lastHb = now
                val p = ByteArray(4)
                val sec = (now / 1000L).toInt()
                putU32(p, 0, sec)
                out.write(encode(T_HEARTBEAT, p))
                out.flush()
            }
            val got = try {
                ins.read(tmp)
            } catch (_: SocketTimeoutException) {
                0
            } catch (_: Exception) {
                return
            }
            if (got < 0) return
            if (got > 0) {
                rb.append(tmp, got)
                if (!parse(rb)) return
            }
        }
    }

    /** @return false 表示这条流已经没法救了（magic 错乱），交给上层重连。 */
    private fun parse(rb: StreamBuf): Boolean {
        while (rb.len >= HDR) {
            if (!rb.matchMagic()) {
                val i = rb.indexOfMagic()
                if (i < 0) {
                    rb.clear()
                    return true
                }
                rb.consume(i)
                continue
            }
            val n = rb.u32(20).toInt()
            if (n < 0 || n > 65536) {
                rb.clear()
                return true
            }
            val total = HDR + n + 4
            if (rb.len < total) return true
            val crc = CRC32()
            crc.update(rb.data, 4, HDR - 4 + n)
            if (crc.value.toInt() == rb.u32(HDR + n).toInt()) {
                dispatch(rb.data[5].toInt() and 0xFF, rb.data, HDR, n)
            }
            rb.consume(total)
        }
        return true
    }

    private fun dispatch(type: Int, buf: ByteArray, off: Int, n: Int) {
        when (type) {
            T_RADAR_UDP -> {
                if (n > 2) {
                    val p = RadarDecoder.u16(buf, off)
                    if (p == 40204) {
                        val f = RadarDecoder.decode(buf, off + 2, n - 2)
                        if (f != null) listener.onFrame(f)
                    }
                }
            }
            T_STATUS -> {
                if (n >= 34) {
                    val at = System.currentTimeMillis()
                    val st = BoardStatus(
                        uptimeMs = RadarDecoder.u32(buf, off),
                        canRx = RadarDecoder.u32(buf, off + 4),
                        canTx = RadarDecoder.u32(buf, off + 8),
                        canErr = RadarDecoder.u32(buf, off + 12),
                        radarPkts = RadarDecoder.u32(buf, off + 16),
                        radarBytes = RadarDecoder.u32(buf, off + 20),
                        dropped = RadarDecoder.u32(buf, off + 24),
                        wifi = buf[off + 28].toInt() and 0xFF,
                        rssi = RadarDecoder.i8(buf, off + 29),
                        canState = if (n >= 40) buf[off + 36].toInt() and 0xFF else 0,
                        statusAge = if (n >= 40) RadarDecoder.u16(buf, off + 38) else 0,
                        atMs = at,
                    )
                    listener.onStatus(st)
                }
            }
            T_CMD_ACK -> {
                if (n >= 6) {
                    val op = buf[off].toInt() and 0xFF
                    val res = buf[off + 1].toInt() and 0xFF
                    val msg = if (n > 6) String(buf, off + 6, n - 6, Charsets.UTF_8).trim() else ""
                    listener.onAck(op, res, msg)
                }
            }
            T_LOG -> if (n > 0) {
                listener.onLog("板卡日志：" + String(buf, off, n, Charsets.UTF_8).trim())
            }
            T_HELLO -> if (n > 0) {
                listener.onState("板卡 HELLO（桥接在线）", true)
            }
        }
    }

    // ------------------------------------------------------------------ 编码
    private fun encode(type: Int, payload: ByteArray): ByteArray {
        val n = payload.size
        val buf = ByteArray(HDR + n + 4)
        System.arraycopy(MAGIC, 0, buf, 0, 4)
        buf[4] = 1
        buf[5] = type.toByte()
        // flags/seq 留 0（板卡侧只校验 magic/version/crc）；ts 用单调时钟，仅作参考
        putU32(buf, 8, 0)
        putU64(buf, 12, System.nanoTime())
        putU32(buf, 20, n)
        System.arraycopy(payload, 0, buf, HDR, n)
        val crc = CRC32()
        crc.update(buf, 4, HDR - 4 + n)
        putU32(buf, HDR + n, crc.value.toInt())
        return buf
    }

    /** 定长环形流缓冲：避免每帧 new ByteArray，也避免 ArrayList 拼接。 */
    private class StreamBuf(cap: Int) {
        var data = ByteArray(cap)
        var len = 0

        fun clear() {
            len = 0
        }

        fun append(src: ByteArray, n: Int) {
            ensure(len + n)
            System.arraycopy(src, 0, data, len, n)
            len += n
        }

        fun consume(n: Int) {
            if (n <= 0) return
            if (n >= len) {
                len = 0
                return
            }
            System.arraycopy(data, n, data, 0, len - n)
            len -= n
        }

        fun u32(off: Int): Long = RadarDecoder.u32(data, off)

        fun matchMagic(): Boolean =
            data[0] == MAGIC[0] && data[1] == MAGIC[1] && data[2] == MAGIC[2] && data[3] == MAGIC[3]

        fun indexOfMagic(): Int {
            val end = len - 4
            var i = 1
            while (i <= end) {
                if (data[i] == MAGIC[0] && data[i + 1] == MAGIC[1] &&
                    data[i + 2] == MAGIC[2] && data[i + 3] == MAGIC[3]
                ) return i
                i++
            }
            return -1
        }

        private fun ensure(need: Int) {
            if (need <= data.size) return
            var cap = data.size
            while (cap < need) cap = cap shl 1
            data = data.copyOf(cap)
        }
    }

    companion object {
        const val HDR = 24
        const val T_HELLO = 0x01
        const val T_HEARTBEAT = 0x02
        const val T_RADAR_UDP = 0x20
        const val T_STATUS = 0x30
        const val T_CMD = 0x40
        const val T_CMD_ACK = 0x41
        const val T_LOG = 0x50

        const val OP_SET_RPM = 0x01
        const val OP_STOP = 0x02
        const val OP_MOTOR_ENABLE = 0x03
        const val OP_MOTOR_DISABLE = 0x04

        fun putU32(b: ByteArray, off: Int, v: Int) {
            b[off] = (v and 0xFF).toByte()
            b[off + 1] = ((v shr 8) and 0xFF).toByte()
            b[off + 2] = ((v shr 16) and 0xFF).toByte()
            b[off + 3] = ((v shr 24) and 0xFF).toByte()
        }

        fun putU64(b: ByteArray, off: Int, v: Long) {
            for (i in 0 until 8) b[off + i] = ((v shr (8 * i)) and 0xFF).toByte()
        }
    }
}

/**
 * 演示数据源：不接硬件也能看界面（对应 Python 的 --src demo）。
 * 合成一条走廊：正前方 ±0.6 m 的两面墙 + 远处端墙。
 */
class DemoSource(private val listener: T40Client.Listener) : RadarSource {
    override val canCmd: Boolean get() = true
    override val name: String get() = "demo"

    @Volatile private var running = false
    private var thread: Thread? = null
    private var rpm = 1200
    private val rnd = java.util.Random(7)

    override fun start() {
        if (running) return
        running = true
        thread = Thread({
            var slot = 0
            var lastStatus = 0L
            var seq = 0
            while (running) {
                val t0 = System.currentTimeMillis()
                val per = (2.0 * Math.PI / 268.0).toFloat()
                val front = Math.PI.toFloat()
                val pts = ArrayList<FloatArray>(16)
                // 每格 1~4 个点：两个侧墙 + 地面杂波
                val k = 1 + rnd.nextInt(3)
                for (i in 0 until k) {
                    val r = 1.2f + rnd.nextFloat() * 6.0f
                    val side = if (rnd.nextBoolean()) 0.6f else -0.6f
                    // 侧墙：把 (0.6 m, r) 极坐标反推成方位/距离
                    val az = front + Math.atan2(side.toDouble(), r.toDouble()).toFloat()
                    val rr = Math.hypot(side.toDouble(), r.toDouble()).toFloat()
                    pts.add(floatArrayOf(az, 0f, rr, 120f + rnd.nextFloat() * 260f))
                }
                if (rnd.nextInt(8) == 0) {
                    // 端墙
                    val az = front + (rnd.nextFloat() - 0.5f) * 1.2f
                    pts.add(floatArrayOf(az, 0f, 7.4f + rnd.nextFloat() * 0.4f, 200f + rnd.nextFloat() * 200f))
                }
                val n = pts.size
                val az = FloatArray(n)
                val el = FloatArray(n)
                val r = FloatArray(n)
                val c3 = FloatArray(n)
                val c5 = FloatArray(n)
                val slotAz = slot * per
                for (i in 0 until n) {
                    az[i] = pts[i][0]
                    el[i] = pts[i][1]
                    r[i] = pts[i][2]
                    c3[i] = pts[i][3]
                    // c5 与方位成正比：c5 = 方位/6283 * 60e6/rpm（和真机同一关系）
                    c5[i] = (az[i] * (60e6f / rpm / 6283.19f)) % 65536f
                }
                listener.onFrame(
                    RadarDecoder.Frame(az, el, r, c3, c5, seq, true)
                )
                seq = (seq + 1) and 0xFFFF
                slot = (slot + 1) % 268

                val now = System.currentTimeMillis()
                if (now - lastStatus >= 1000L) {
                    lastStatus = now
                    listener.onStatus(
                        T40Client.BoardStatus(
                            uptimeMs = now,
                            canRx = 0, canTx = 0, canErr = 0,
                            radarPkts = (now / 25).toLong(),
                            radarBytes = (now / 6),
                            dropped = 0,
                            wifi = 1, rssi = -1, canState = 1, statusAge = 0,
                            atMs = now,
                        )
                    )
                }
                val dt = 50 - (System.currentTimeMillis() - t0)
                if (dt > 0) try {
                    Thread.sleep(dt)
                } catch (_: InterruptedException) {
                    break
                }
            }
        }, "t40-demo").apply {
            isDaemon = true
            start()
        }
    }

    override fun stop() {
        running = false
        thread?.interrupt()
        thread = null
    }

    override fun setRpm(rpm: Int) {
        this.rpm = rpm.coerceIn(300, 1800)
        listener.onAck(T40Client.OP_SET_RPM, 0, "demo")
    }

    override fun motorEnable() = listener.onAck(T40Client.OP_MOTOR_ENABLE, 0, "demo")
    override fun motorDisable() = listener.onAck(T40Client.OP_MOTOR_DISABLE, 0, "demo")
    override fun stopMotor() = listener.onAck(T40Client.OP_STOP, 0, "demo")
}
