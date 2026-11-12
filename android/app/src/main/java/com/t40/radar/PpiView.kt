package com.t40.radar

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PointF
import android.os.SystemClock
import android.util.AttributeSet
import android.view.View
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * PPI 俯视点云视图 —— 对应 host/t40_cloud_view.py 里 pyqtgraph 那套画法，纯 Canvas 重画。
 *
 * 坐标系与 PC 端一致（这是现场锚定过的，别自己改）：
 *   * 点以**米**存，屏幕坐标现算 ⇒ 改量程不用重投影、历史点不用清；
 *   * 改朝向（旋转角 / 方位左右手）必须清历史点 —— 老点是按老朝向算的，转不回去；
 *   * 默认「整幅画面逆时针 180° + 方位逆时针」，0° = 雷达正前方 = 那面 3.6 m 墙；
 *   * 121° 无数据扇区（BLIND）画成灰色楔形。
 */
class PpiView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    // ---------------------------------------------------------------- 可调参数
    /** 量程（米），2–70。 */
    var rangeM = 8f
        set(v) {
            val x = v.coerceIn(2f, 70f)
            if (x != field) {
                field = x
                invalidate()
            }
        }

    /** 幅度阈值 c3；只影响配色/过滤，不重投影。 */
    var threshold = 80f
        set(v) {
            if (v != field) {
                field = v
                invalidate()
            }
        }

    /** 累积圈数，0 = 只看当前圈。 */
    var histFrames = 30
        set(v) {
            val x = v.coerceIn(0, 400)
            if (x != field) {
                field = x
                if (x == 0) hist.clear() else hist.trim(x)
                invalidate()
            }
        }

    /** 画面逆时针旋转角（度）。 */
    var rotDeg = 180f
        set(v) {
            val x = ((v % 360f) + 360f) % 360f
            if (x != field) {
                field = x
                clearHistory()
                invalidate()
            }
        }

    /** 方位逆时针（true）还是顺时针（false）。 */
    var ccw = true
        set(v) {
            if (v != field) {
                field = v
                clearHistory()
                invalidate()
            }
        }

    /** 是否标出 121° 无数据扇区。 */
    var showBlind = true
        set(v) {
            if (v != field) {
                field = v
                invalidate()
            }
        }

    var paused = false
        set(v) {
            field = v
            invalidate()
        }

    // ---------------------------------------------------------------- 统计（界面读）
    var frameCount = 0L
        private set
    var ptsTotal = 0L
        private set
    var ptsLast = 0
        private set
    var lastSeq = -1
        private set
    var lastSumOk = true
        private set

    // ---------------------------------------------------------------- 内部状态
    private val density = resources.displayMetrics.density
    private val scaled = resources.displayMetrics.scaledDensity

    private var cx = 0f
    private var cy = 0f
    private var scale = 1f

    private val rotRad: Float get() = Math.toRadians(rotDeg.toDouble()).toFloat()
    private val clockwise: Boolean get() = !ccw

    private var curX = FloatArray(0)
    private var curY = FloatArray(0)
    private var curC3 = FloatArray(0)
    private var curN = 0

    private var tmpX = FloatArray(0)
    private var tmpY = FloatArray(0)

    private val hist = HistRing(MAX_HIST_PTS)

    private var lastInvalidate = 0L

    // ---------------------------------------------------------------- 画笔
    private val bgPaint = Paint().apply { color = Color.BLACK; style = Paint.Style.FILL }

    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xDC46505F.toInt()
        style = Paint.Style.STROKE
        strokeWidth = dp(1f)
    }
    private val dash = DashPathEffect(floatArrayOf(dp(3f), dp(3f)), 0f)

    private val spokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xAA373E4B.toInt()
        style = Paint.Style.STROKE
        strokeWidth = dp(1f)
    }

    private val blindFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0x329696A0
        style = Paint.Style.FILL
    }
    private val blindStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0x78787D87
        style = Paint.Style.STROKE
        strokeWidth = dp(1f)
    }

    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFF78879B.toInt()
        textSize = scaled * 10f
        textAlign = Paint.Align.CENTER
    }
    private val dirPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFFCDDCF0.toInt()
        textSize = scaled * 12f
        textAlign = Paint.Align.CENTER
        isFakeBoldText = true
    }
    private val centerFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFF141414.toInt()
        style = Paint.Style.FILL
    }
    private val centerStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
    }

    private val histPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0x464678BE
        strokeCap = Paint.Cap.ROUND
    }

    /** 16 级配色，色带与 Python 的 _RAMP 完全一致。 */
    private val levelPaint = Array(NLEV) { i ->
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = ramp(i / (NLEV - 1f))
            strokeCap = Paint.Cap.ROUND
        }
    }
    private val bucket = Array(NLEV) { FloatArray(2 * 512) }
    private val bucketN = IntArray(NLEV)

    // ---------------------------------------------------------------- 数据入口
    /**
     * 新到一帧（必须在主线程调用）。返回**过阈值之后**保留下来的点数
     * （统计「点/秒」用它，口径和 PC 端一致；ptsLast 是本帧总点数）。
     */
    fun pushFrame(f: RadarDecoder.Frame): Int {
        if (paused) return 0
        frameCount++
        lastSeq = f.seq
        lastSumOk = f.sumOk
        val n = f.n
        ptsLast = n
        ensureCur(n)
        RadarDecoder.project(f.az, f.el, f.r, n, clockwise, rotRad, curX, curY)
        System.arraycopy(f.c3, 0, curC3, 0, n)
        curN = n

        // 阈值过滤：历史点按米存，改量程不用重算
        val lo = threshold
        var m = 0
        if (histFrames > 0) {
            ensureTmp(n)
            for (k in 0 until n) {
                if (curC3[k] >= lo) {
                    tmpX[m] = curX[k]
                    tmpY[m] = curY[k]
                    m++
                }
            }
            if (m > 0) hist.addFrame(tmpX, tmpY, m, histFrames)
        } else {
            for (k in 0 until n) if (curC3[k] >= lo) m++
        }
        ptsTotal += m.toLong()

        val now = SystemClock.uptimeMillis()
        if (now - lastInvalidate >= DRAW_MIN_MS) {
            lastInvalidate = now
            invalidate()
        }
        return m
    }

    fun clearHistory() {
        hist.clear()
        curN = 0
        invalidate()
    }

    fun resetCounters() {
        frameCount = 0
        ptsTotal = 0
        ptsLast = 0
        lastSeq = -1
        lastSumOk = true
    }

    // ---------------------------------------------------------------- 绘制
    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        cx = w / 2f
        cy = h / 2f
        scale = (min(w, h) / 2f - dp(2f)) / (rangeM * 1.18f)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (width == 0 || height == 0) return
        scale = (min(width, height) / 2f - dp(2f)) / (rangeM * 1.18f)

        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), bgPaint)
        drawGrid(canvas)
        drawHistory(canvas)
        drawCurrent(canvas)
        drawCenter(canvas)
    }

    private fun drawGrid(canvas: Canvas) {
        val R = rangeM
        val stepI = if (R <= 10f) 1 else if (R <= 30f) 5 else 10
        val step = stepI.toFloat()
        val ta = Math.toRadians(215.0).toFloat() - rotRad

        var rr = step
        while (rr <= R + 1e-3f) {
            val rpx = rr * scale
            ringPaint.pathEffect = if (rr.toInt() % (2 * stepI) != 0) dash else null
            canvas.drawCircle(cx, cy, rpx, ringPaint)
            canvas.drawText(
                fmtMeters(rr),
                cx + sin(ta) * rpx,
                cy - cos(ta) * rpx + labelPaint.textSize * 0.35f,
                labelPaint
            )
            rr += step
        }
        ringPaint.pathEffect = null

        var a = 0
        while (a < 360) {
            val p = polar(Math.toRadians(a.toDouble()).toFloat(), R * scale)
            canvas.drawLine(cx, cy, p.x, p.y, spokePaint)
            a += 30
        }

        if (showBlind) {
            val path = Path()
            path.moveTo(cx, cy)
            val steps = 96
            for (i in 0..steps) {
                val th = BLIND0 + (BLIND1 - BLIND0) * i / steps
                val s = if (ccw) -th else th
                val t = s - rotRad
                path.lineTo(
                    cx + sin(t) * R * scale,
                    cy - cos(t) * R * scale
                )
            }
            path.close()
            canvas.drawPath(path, blindFill)
            canvas.drawPath(path, blindStroke)
        }

        // 前/后/左/右 是"雷达的方位"，跟着画面朝向一起转
        drawDir(canvas, "前 0°", 0f)
        drawDir(canvas, "后 180°", 180f)
        drawDir(canvas, "右 90°", 90f)
        drawDir(canvas, "左 270°", 270f)
    }

    private fun drawDir(canvas: Canvas, text: String, azDeg: Float) {
        val p = polar(Math.toRadians(azDeg.toDouble()).toFloat(), rangeM * 1.10f * scale)
        canvas.drawText(text, p.x, p.y + dirPaint.textSize * 0.35f, dirPaint)
    }

    private fun drawHistory(canvas: Canvas) {
        if (hist.count <= 0) return
        val n = hist.count
        val cap = hist.cap
        val head = hist.head
        canvas.save()
        canvas.translate(cx, cy)
        canvas.scale(scale, -scale)
        histPaint.strokeWidth = dp(1.9f) / scale
        val end = head + n
        if (end <= cap) {
            canvas.drawPoints(hist.buf, head * 2, n * 2, histPaint)
        } else {
            val first = cap - head
            canvas.drawPoints(hist.buf, head * 2, first * 2, histPaint)
            canvas.drawPoints(hist.buf, 0, (n - first) * 2, histPaint)
        }
        canvas.restore()
    }

    private fun drawCurrent(canvas: Canvas) {
        if (paused || curN <= 0) return
        for (i in 0 until NLEV) bucketN[i] = 0
        val lo = threshold
        for (k in 0 until curN) {
            val t = ((curC3[k] - lo) / 300f).coerceIn(0f, 0.999f)
            val lv = (t * NLEV).toInt()
            var b = bucket[lv]
            val c = bucketN[lv]
            if (c * 2 + 2 > b.size) {
                // 同幅度点太多：这块扩一倍再放（不丢点）
                val nb = FloatArray(b.size * 2)
                System.arraycopy(b, 0, nb, 0, b.size)
                bucket[lv] = nb
                b = nb
            }
            b[c * 2] = curX[k]
            b[c * 2 + 1] = curY[k]
            bucketN[lv] = c + 1
        }
        canvas.save()
        canvas.translate(cx, cy)
        canvas.scale(scale, -scale)
        val w = dp(2.7f) / scale
        for (i in 0 until NLEV) {
            val c = bucketN[i]
            if (c == 0) continue
            val p = levelPaint[i]
            p.strokeWidth = w
            canvas.drawPoints(bucket[i], 0, c * 2, p)
        }
        canvas.restore()
    }

    private fun drawCenter(canvas: Canvas) {
        canvas.drawCircle(cx, cy, dp(4f), centerFill)
        canvas.drawCircle(cx, cy, dp(4f), centerStroke)
    }

    // ---------------------------------------------------------------- 工具
    /** 雷达方位角 → 屏幕坐标（和点云同一套变换）。 */
    private fun polar(azRad: Float, radiusPx: Float): PointF {
        val s = if (ccw) -azRad else azRad
        val t = s - rotRad
        return PointF(cx + sin(t) * radiusPx, cy - cos(t) * radiusPx)
    }

    private fun fmtMeters(v: Float): String =
        if (v == v.toInt().toFloat()) "%d m".format(v.toInt()) else "%.1f m".format(v)

    private fun dp(v: Float) = v * density

    private fun ramp(t: Float): Int {
        var i = 0
        while (i < RAMP_P.size - 2 && t > RAMP_P[i + 1]) i++
        val span = RAMP_P[i + 1] - RAMP_P[i]
        val f = if (span <= 0f) 0f else ((t - RAMP_P[i]) / span).coerceIn(0f, 1f)
        val a = RAMP_C[i]
        val b = RAMP_C[i + 1]
        val r = (((a shr 16) and 0xFF) + (((b shr 16) and 0xFF) - ((a shr 16) and 0xFF)) * f).toInt()
        val g = (((a shr 8) and 0xFF) + (((b shr 8) and 0xFF) - ((a shr 8) and 0xFF)) * f).toInt()
        val bl = ((a and 0xFF) + ((b and 0xFF) - (a and 0xFF)) * f).toInt()
        return Color.rgb(r, g, bl)
    }

    private fun ensureCur(n: Int) {
        if (curX.size >= n) return
        val cap = maxOf(n, curX.size * 2 + 64)
        curX = FloatArray(cap)
        curY = FloatArray(cap)
        curC3 = FloatArray(cap)
    }

    private fun ensureTmp(n: Int) {
        if (tmpX.size >= n) return
        val cap = maxOf(n, tmpX.size * 2 + 64)
        tmpX = FloatArray(cap)
        tmpY = FloatArray(cap)
    }

    /**
     * 历史点环形缓冲：按米存，帧边界单独记，超预算/超圈数就整帧丢最老的。
     * 和 Python 的「分块追加」是同一个思路 —— 每帧代价只和本帧点数有关。
     */
    private class HistRing(val cap: Int) {
        val buf = FloatArray(cap * 2)
        var head = 0
        var count = 0
        private val frames = ArrayDeque<Int>()

        fun clear() {
            head = 0
            count = 0
            frames.clear()
        }

        fun addFrame(x: FloatArray, y: FloatArray, n: Int, maxFrames: Int) {
            if (n <= 0) return
            while (count + n > cap && frames.isNotEmpty()) dropOldest()
            if (count + n > cap) {
                clear()
                if (n > cap) return
            }
            for (k in 0 until n) {
                val idx = head + count
                val i2 = (if (idx >= cap) idx - cap else idx) * 2
                buf[i2] = x[k]
                buf[i2 + 1] = y[k]
                count++
            }
            frames.addLast(n)
            trim(maxFrames)
        }

        fun trim(maxFrames: Int) {
            if (maxFrames <= 0) {
                clear()
                return
            }
            while (frames.size > maxFrames) dropOldest()
        }

        private fun dropOldest() {
            if (frames.isEmpty()) return
            val k = frames.removeFirst()
            head += k
            if (head >= cap) head -= cap
            count -= k
            if (count < 0) count = 0
        }
    }

    companion object {
        private const val NLEV = 16
        private const val MAX_HIST_PTS = 80000
        private const val DRAW_MIN_MS = 25L

        /** 无数据扇区（rad），正中 ≈ 0°（正前方），宽 121°。 */
        private const val BLIND0 = 5.204f
        private const val BLIND1 = 7.314f

        private val RAMP_P = floatArrayOf(0f, 0.20f, 0.40f, 0.60f, 0.75f, 0.88f, 1.00f)
        private val RAMP_C = intArrayOf(
            0x000004, 0x280B54, 0x65156E, 0x9F2A63, 0xD44842, 0xF57D15, 0xFCFFA4
        )
    }
}
