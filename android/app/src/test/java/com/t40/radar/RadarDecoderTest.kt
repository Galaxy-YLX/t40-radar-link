package com.t40.radar

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 解码器对拍测试。
 *
 * 数据来源：`android/tools/make_testdata.py` 从真实 pcap（captures/collect_run1/06_pcap0.pcap）
 * 里抽 60 帧 0x00F0，期望值由 **PC 端那份已经在现场跑通的 Python 解码器**算出来。
 * 这里逐帧比对，任何偏移/量纲写错都会立刻暴露。
 */
class RadarDecoderTest {

    private class Expect(
        val n: Int, val sumOk: Boolean, val seq: Int,
        val az0: Double, val el0: Double, val r0: Double,
        val c30: Double, val c50: Double, val rpm: Double,
    )

    private fun frames(): List<ByteArray> {
        val ins = javaClass.getResourceAsStream("/frames.bin")
        assertNotNull("frames.bin 不在测试资源里", ins)
        val all = ins!!.readBytes()
        val out = ArrayList<ByteArray>()
        var p = 0
        while (p + 4 <= all.size) {
            val n = (all[p].toInt() and 0xFF) or ((all[p + 1].toInt() and 0xFF) shl 8) or
                    ((all[p + 2].toInt() and 0xFF) shl 16) or ((all[p + 3].toInt() and 0xFF) shl 24)
            p += 4
            out.add(all.copyOfRange(p, p + n))
            p += n
        }
        return out
    }

    private fun expects(): List<Expect> {
        val ins = javaClass.getResourceAsStream("/expected.txt")
        assertNotNull("expected.txt 不在测试资源里", ins)
        val out = ArrayList<Expect>()
        ins!!.bufferedReader().forEachLine { line ->
            if (line.startsWith("#") || line.isBlank()) return@forEachLine
            val f = line.trim().split(Regex("\\s+"))
            out.add(
                Expect(
                    n = f[0].toInt(), sumOk = f[1] == "1", seq = f[2].toInt(),
                    az0 = f[3].toDouble(), el0 = f[4].toDouble(), r0 = f[5].toDouble(),
                    c30 = f[6].toDouble(), c50 = f[7].toDouble(), rpm = f[8].toDouble(),
                )
            )
        }
        return out
    }

    @Test
    fun decodesRealFramesExactlyLikePython() {
        val bufs = frames()
        val exp = expects()
        assertEquals("帧数不一致", exp.size, bufs.size)
        assertTrue("测试数据太少", exp.size >= 10)

        var pts = 0L
        for (i in exp.indices) {
            val e = exp[i]
            val f = RadarDecoder.decode(bufs[i], 0, bufs[i].size)
            assertNotNull("第 $i 帧解不出来", f)
            f!!
            assertEquals("第 $i 帧点数", e.n, f.n)
            assertEquals("第 $i 帧校验和", e.sumOk, f.sumOk)
            assertEquals("第 $i 帧 seq", e.seq, f.seq)
            assertEquals("第 $i 帧 az0", e.az0, f.az[0].toDouble(), 1e-4)
            assertEquals("第 $i 帧 el0", e.el0, f.el[0].toDouble(), 1e-4)
            assertEquals("第 $i 帧 r0", e.r0, f.r[0].toDouble(), 2e-3)
            assertEquals("第 $i 帧 c3[0]", e.c30, f.c3[0].toDouble(), 0.5)
            assertEquals("第 $i 帧 c5[0]", e.c50, f.c5[0].toDouble(), 0.5)
            val rpm = RadarDecoder.rpmFromC5(f.az, f.c5).toDouble()
            assertEquals("第 $i 帧 rpm", e.rpm, rpm, maxOf(0.5, e.rpm * 1e-3))
            pts += f.n
        }
        println("对拍通过：${exp.size} 帧 / $pts 点")
    }

    /** 负载前面可能带别的头（UDP 里 DUML 不一定在 0 偏移）。 */
    @Test
    fun findsDumlMagicAtOffset() {
        val raw = frames()[0]
        val junk = byteArrayOf(0x11, 0x22, 0x33, 0x44, 0x55)
        val buf = junk + raw
        val a = RadarDecoder.decode(raw, 0, raw.size)!!
        val b = RadarDecoder.decode(buf, 0, buf.size)!!
        assertEquals(a.n, b.n)
        assertEquals(a.seq, b.seq)
        assertEquals(a.az[7].toDouble(), b.az[7].toDouble(), 1e-6)
    }

    @Test
    fun rejectsGarbage() {
        assertNull(RadarDecoder.decode(ByteArray(0), 0, 0))
        assertNull(RadarDecoder.decode(ByteArray(64) { 0x5A }, 0, 64))
        // magic 对了但记录长度不自洽
        val bad = ByteArray(64)
        bad[0] = 0x03; bad[1] = 0x10; bad[2] = 0x07
        bad[13] = 7 // nbytes = 7，不是 12 的倍数
        assertNull(RadarDecoder.decode(bad, 0, 64))
    }

    /**
     * 投影约定（现场锚定过，PC 端也是这套）：
     * 方位 0° 朝上；顺时针时 90° 在右；逆时针时 90° 在左；旋转 180° 整幅倒过来。
     */
    @Test
    fun projectionConvention() {
        val az = floatArrayOf(0f, (Math.PI / 2).toFloat(), Math.PI.toFloat())
        val el = floatArrayOf(0f, 0f, 0f)
        val r = floatArrayOf(1f, 1f, 1f)
        val x = FloatArray(3)
        val y = FloatArray(3)

        RadarDecoder.project(az, el, r, 3, clockwise = true, rotRad = 0f, outX = x, outY = y)
        assertEquals(0f, x[0], 1e-5f); assertEquals(1f, y[0], 1e-5f)     // 上
        assertEquals(1f, x[1], 1e-5f); assertEquals(0f, y[1], 1e-5f)     // 右
        assertEquals(0f, x[2], 1e-5f); assertEquals(-1f, y[2], 1e-5f)    // 下

        // 逆时针：90° 应该跑到左边
        RadarDecoder.project(az, el, r, 3, clockwise = false, rotRad = 0f, outX = x, outY = y)
        assertEquals(-1f, x[1], 1e-5f); assertEquals(0f, y[1], 1e-5f)

        // 画面逆时针转 180°：0° 跑到下方
        RadarDecoder.project(
            az, el, r, 3, clockwise = true,
            rotRad = Math.PI.toFloat(), outX = x, outY = y
        )
        assertEquals(0f, x[0], 1e-4f); assertEquals(-1f, y[0], 1e-4f)
    }
}
