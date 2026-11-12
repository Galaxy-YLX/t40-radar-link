package com.t40.radar

/**
 * T40 雷达 0x00F0 点云负载解码。
 *
 * 这是 host/t40_cloud_view.py 里 `decode_00f0` / `points_xy` / `rpm_from_c5` 的 Kotlin 版，
 * 算法逐行对齐，改一边记得改另一边（tools/verify_decoder.py 会拿同一批 pcap 对拍）。
 *
 * 负载结构（实测，见 T40雷达0x00F0逆向总结_2026-11-10.md）：
 *
 *     [0..3)    DUML magic 03 10 07
 *     [3..5)    seq           (u16)
 *     [13..15)  记录区字节数 N (u16，= 记录数 × 12)
 *     [17]      记录区字节和 & 0xFF
 *     [18]      header[1..18) 字节和 & 0xFF
 *     [19..)    N/12 条记录，每条 6 个 u16：
 *               方位 mrad / 俯仰 mrad / 距离 mm / c3 幅度 / c4 / c5 时戳
 *
 * 注意负载前面可能带别的头（UDP 里 DUML 不一定在 0 偏移），所以先找 magic 再解。
 */
object RadarDecoder {

    private val MAGIC = byteArrayOf(0x03, 0x10, 0x07)

    const val HDR19 = 19
    const val REC12 = 12
    const val TWO_PI = 6.28319f

    /** 一帧点云。数组长度 = n，单位：方位/俯仰 rad、距离 m、c3 与 c5 是固件原生量。 */
    class Frame(
        val az: FloatArray,
        val el: FloatArray,
        val r: FloatArray,
        val c3: FloatArray,
        val c5: FloatArray,
        val seq: Int,
        val sumOk: Boolean,
    ) {
        val n: Int get() = az.size
    }

    /** payload[off, off+len) → Frame；不是 0x00F0 或长度不自洽返回 null。 */
    fun decode(buf: ByteArray, off: Int, len: Int): Frame? {
        if (len <= 0) return null
        val st = findMagic(buf, off, len)
        if (st < 0) return null
        val avail = len - (st - off)
        if (avail < HDR19) return null

        val nbytes = u16(buf, st + 13)
        if (nbytes == 0 || nbytes % REC12 != 0 || HDR19 + nbytes > avail) return null
        val n = nbytes / REC12
        val raw = st + HDR19

        var sumRaw = 0
        for (i in 0 until nbytes) sumRaw += buf[raw + i].toInt() and 0xFF
        var sumHdr = 0
        for (i in 1 until 18) sumHdr += buf[st + i].toInt() and 0xFF
        val sumOk = (buf[st + 17].toInt() and 0xFF) == (sumRaw and 0xFF) &&
                (buf[st + 18].toInt() and 0xFF) == (sumHdr and 0xFF)

        val az = FloatArray(n)
        val el = FloatArray(n)
        val r = FloatArray(n)
        val c3 = FloatArray(n)
        val c5 = FloatArray(n)
        var k = 0
        while (k < n) {
            val p = raw + k * REC12
            az[k] = u16(buf, p) / 1000.0f
            el[k] = u16(buf, p + 2) / 1000.0f - TWO_PI
            r[k] = u16(buf, p + 4) / 1000.0f
            c3[k] = u16(buf, p + 6).toFloat()
            c5[k] = u16(buf, p + 10).toFloat()
            k++
        }
        return Frame(az, el, r, c3, c5, u16(buf, st + 3), sumOk)
    }

    /**
     * 球坐标 → 俯视平面（米）。与 Python points_xy 一致：
     * 0° 朝上（+y），clockwise 时方位顺时针增加，rotRad 是**画面逆时针**旋转角。
     */
    fun project(
        az: FloatArray, el: FloatArray, r: FloatArray, n: Int,
        clockwise: Boolean, rotRad: Float,
        outX: FloatArray, outY: FloatArray,
    ) {
        var k = 0
        while (k < n) {
            val d = r[k] * kotlin.math.cos(el[k])
            var s = if (clockwise) az[k] else -az[k]
            s -= rotRad
            outX[k] = d * kotlin.math.sin(s)
            outY[k] = d * kotlin.math.cos(s)
            k++
        }
    }

    /**
     * 同一帧里 c5 对方位的斜率 ⇒ 转速。µs 与 mrad 都是固件原生量，免标定。
     * 返回 0 表示这一帧算不出来（点太少/方位退化），调用方保留上一次值。
     */
    fun rpmFromC5(az: FloatArray, c5: FloatArray): Float {
        val n = az.size
        if (n < 12) return 0f
        // 每个方位格只取第一次出现的 c5（一格里可能有多条记录）
        val key = IntArray(n)
        var uniq = 0
        val seen = HashMap<Int, Float>(n * 2)
        for (k in 0 until n) {
            val a = (az[k] * 1000f).toInt()
            key[k] = a
            if (!seen.containsKey(a)) {
                seen[a] = c5[k]
                uniq++
            }
        }
        if (uniq < 8) return 0f
        val xs = seen.keys.toIntArray()
        xs.sort()

        var base = 0.0
        var prev = seen[xs[0]]!!.toDouble()
        val ys = DoubleArray(xs.size)
        for (i in xs.indices) {
            val t = seen[xs[i]]!!.toDouble()
            var d = (t - prev) % 65536.0
            if (d < 0) d += 65536.0
            if (d > 32768.0) d -= 65536.0
            base += d
            prev = t
            ys[i] = base
        }
        val m = xs.size
        var mx = 0.0
        var my = 0.0
        for (i in 0 until m) {
            mx += xs[i]
            my += ys[i]
        }
        mx /= m
        my /= m
        var num = 0.0
        var den = 0.0
        for (i in 0 until m) {
            val dx = xs[i] - mx
            num += dx * (ys[i] - my)
            den += dx * dx
        }
        if (den <= 0.0 || num <= 0.0) return 0f
        return (60e6 / (num / den * 6283.19)).toFloat()
    }

    fun u16(b: ByteArray, i: Int): Int =
        (b[i].toInt() and 0xFF) or ((b[i + 1].toInt() and 0xFF) shl 8)

    fun u32(b: ByteArray, i: Int): Long =
        ((b[i].toLong() and 0xFF) or ((b[i + 1].toLong() and 0xFF) shl 8) or
                ((b[i + 2].toLong() and 0xFF) shl 16) or ((b[i + 3].toLong() and 0xFF) shl 24))

    fun i8(b: ByteArray, i: Int): Int = b[i].toInt()

    private fun findMagic(buf: ByteArray, off: Int, len: Int): Int {
        val end = off + len - 3
        var i = off
        while (i <= end) {
            if (buf[i] == MAGIC[0] && buf[i + 1] == MAGIC[1] && buf[i + 2] == MAGIC[2]) return i
            i++
        }
        return -1
    }
}
