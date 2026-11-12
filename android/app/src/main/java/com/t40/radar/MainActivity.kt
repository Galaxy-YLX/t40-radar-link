package com.t40.radar

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.Switch
import android.widget.TextView

/**
 * 主界面：一个 PPI 视图 + 顶部状态条 + 底部可折叠控制面板。
 *
 * 和 PC 端 host/t40_cloud_view.py 的分工完全对应：
 *   PpiView 负责画，T40Client 负责收/发，这里只做「连线 + 状态判读」。
 * 状态判读那几条（STATUS 不来 / wifi=0 / 没有 0x00F0）都是现场踩出来的，
 * 症状和原因离得很远，别随手删。
 *
 * 两个容易踩的地方（都踩过）：
 *   1. 回调全部来自后台线程，而且**旧连接的回调会在断开之后才飘到主线程**。
 *      所以每次连接都发一个 token，回调里先比对 token，对不上直接丢 ——
 *      否则界面会被一条迟到的"已连上"钉死在"板卡卡住了，断电重启"的假故障态。
 *   2. 链路状态只认 onState 的 linkUp 标志，**不要拿文本里有没有"失败/断开"去猜**
 *      —— 板卡的日志（CAN 打开失败等）也走同一条通道，会把好链路打成未连接。
 */
class MainActivity : Activity() {

    private lateinit var ppi: PpiView
    private lateinit var banner: TextView
    private lateinit var stats: TextView
    private lateinit var scrollPanel: ScrollView
    private lateinit var btnConnect: Button
    private lateinit var btnPanel: Button
    private lateinit var btnPause: Button
    private lateinit var etHost: EditText
    private lateinit var etPort: EditText
    private lateinit var tvRange: TextView
    private lateinit var tvThr: TextView
    private lateinit var tvHist: TextView
    private lateinit var tvRot: TextView
    private lateinit var tvRpm: TextView
    private lateinit var tvAck: TextView
    private lateinit var swCcw: Switch
    private lateinit var swBlind: Switch

    private val ui = Handler(Looper.getMainLooper())
    private var src: RadarSource? = null
    private val prefs by lazy { getSharedPreferences("t40", MODE_PRIVATE) }

    /** 每次连接/断开 +1；回调里比对，用来丢掉旧连接迟到的回调。 */
    private var srcToken = 0

    // ---- 链路状态 ----
    private var connected = false
    private var linkMsg = "未连接"
    private var demo = false

    // ---- 统计 ----
    private var lastFrameMs = 0L
    private var lastStatusMs = 0L
    private var st: T40Client.BoardStatus? = null
    private var prevBytes = -1L
    private var prevBytesMs = 0L
    private var kbps = 0f
    private var rpm = 0f
    private var ackText = "—"

    private val frameStamps = ArrayDeque<Long>()
    private var ptsAccum = 0L
    private var ptsAccumMs = 0L
    private var fps = 0f
    private var pps = 0f

    private var lastBanner = ""
    private var lastBannerColor = 0

    // 主线程只处理「最新一帧」，避免 UI 卡时把队列撑爆
    @Volatile private var pending: RadarDecoder.Frame? = null
    @Volatile private var drainScheduled = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        immersive()

        ppi = findViewById(R.id.ppi)
        banner = findViewById(R.id.banner)
        stats = findViewById(R.id.stats)
        scrollPanel = findViewById(R.id.scrollPanel)
        btnConnect = findViewById(R.id.btnConnect)
        btnPanel = findViewById(R.id.btnPanel)
        btnPause = findViewById(R.id.btnPause)
        etHost = findViewById(R.id.etHost)
        etPort = findViewById(R.id.etPort)
        tvRange = findViewById(R.id.tvRange)
        tvThr = findViewById(R.id.tvThr)
        tvHist = findViewById(R.id.tvHist)
        tvRot = findViewById(R.id.tvRot)
        tvRpm = findViewById(R.id.tvRpm)
        tvAck = findViewById(R.id.tvAck)
        swCcw = findViewById(R.id.swCcw)
        swBlind = findViewById(R.id.swBlind)

        // 先装监听、再恢复取值：restorePrefs 里设 progress 会走一遍 onChange，
        // 顺便把面板上的数值文本也刷对（SeekBar 的 max 必须已经设好，否则会被夹到 100）
        wireUi()
        restorePrefs()
        ui.postDelayed(ticker, 500)
    }

    // ------------------------------------------------------------------ 界面装配
    private fun restorePrefs() {
        etHost.setText(prefs.getString("host", "192.168.9.1"))
        etPort.setText(prefs.getInt("port", 46000).toString())

        ppi.rangeM = prefs.getFloat("range", 8f)
        ppi.threshold = prefs.getFloat("thr", 80f)
        ppi.histFrames = prefs.getInt("hist", 30)
        ppi.showBlind = prefs.getBoolean("blind", true)

        (findViewById<SeekBar>(R.id.sbRange)).progress = (ppi.rangeM - 2f).toInt()
        tvRange.text = "%.0f m".format(ppi.rangeM)
        (findViewById<SeekBar>(R.id.sbThr)).progress = ppi.threshold.toInt()
        tvThr.text = ppi.threshold.toInt().toString()
        (findViewById<SeekBar>(R.id.sbHist)).progress = ppi.histFrames
        tvHist.text = "${ppi.histFrames} 圈"
        swBlind.isChecked = ppi.showBlind

        val rot = prefs.getFloat("rot", 180f)
        (findViewById<SeekBar>(R.id.sbRot)).progress = (rot / 10f).toInt()
        tvRot.text = "%.0f°".format(rot)
        val ccw = prefs.getBoolean("ccw", true)
        swCcw.isChecked = ccw
        // 朝向必须在其它显示参数之后设：它的 setter 会清历史
        ppi.rotDeg = rot
        ppi.ccw = ccw

        (findViewById<SeekBar>(R.id.sbRpm)).progress = (1200 - 300) / 30
        tvRpm.text = "1200 rpm"
    }

    private fun wireUi() {
        btnConnect.setOnClickListener {
            if (src != null) disconnect() else connect()
        }

        btnPanel.setOnClickListener {
            val show = scrollPanel.visibility != View.VISIBLE
            scrollPanel.visibility = if (show) View.VISIBLE else View.GONE
            btnPanel.text = if (show) "收起" else "面板"
        }

        btnPause.setOnClickListener {
            ppi.paused = !ppi.paused
            btnPause.text = if (ppi.paused) "继续" else "暂停"
        }

        findViewById<Button>(R.id.btnClear).setOnClickListener {
            ppi.clearHistory()
            ppi.resetCounters()
            resetStats()
        }

        findViewById<Button>(R.id.btnDemo).setOnClickListener {
            if (demo) {
                disconnect()
            } else {
                disconnect()
                val token = srcToken
                val d = DemoSource(newListener(token))
                src = d
                demo = true
                connected = true
                linkMsg = "演示数据（没有硬件）"
                btnConnect.text = "断开"
                resetStats()
                d.start()
                updateBanner()
            }
        }

        findViewById<Button>(R.id.btnWifi).setOnClickListener {
            startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
        }

        seek(R.id.sbRange, 0, 68) { p ->
            val v = p + 2f
            ppi.rangeM = v
            tvRange.text = "%.0f m".format(v)
            prefs.edit().putFloat("range", v).apply()
        }
        seek(R.id.sbThr, 0, 2000) { p ->
            ppi.threshold = p.toFloat()
            tvThr.text = p.toString()
            prefs.edit().putFloat("thr", p.toFloat()).apply()
        }
        seek(R.id.sbHist, 0, 400) { p ->
            ppi.histFrames = p
            tvHist.text = "$p 圈"
            prefs.edit().putInt("hist", p).apply()
        }
        seek(R.id.sbRot, 0, 35) { p ->
            val v = p * 10f
            ppi.rotDeg = v
            tvRot.text = "%.0f°".format(v)
            prefs.edit().putFloat("rot", v).apply()
        }
        seek(R.id.sbRpm, 0, 50) { p ->
            tvRpm.text = "${300 + p * 30} rpm"
        }

        swCcw.setOnCheckedChangeListener { _, on ->
            ppi.ccw = on
            prefs.edit().putBoolean("ccw", on).apply()
        }
        swBlind.setOnCheckedChangeListener { _, on ->
            ppi.showBlind = on
            prefs.edit().putBoolean("blind", on).apply()
        }

        findViewById<Button>(R.id.btnRpm).setOnClickListener {
            val v = 300 + findViewById<SeekBar>(R.id.sbRpm).progress * 30
            sendCmd("设置转速 $v") { it.setRpm(v) }
        }
        preset(R.id.btnP600, 600)
        preset(R.id.btnP900, 900)
        preset(R.id.btnP1200, 1200)
        preset(R.id.btnP1500, 1500)
        preset(R.id.btnP1800, 1800)

        findViewById<Button>(R.id.btnMotorOn).setOnClickListener {
            sendCmd("马达使能") { it.motorEnable() }
        }
        findViewById<Button>(R.id.btnMotorOff).setOnClickListener {
            confirm("停掉雷达马达？停转后就没有点云了。") { sendCmd("马达停") { s -> s.motorDisable() } }
        }
        findViewById<Button>(R.id.btnStopMotor).setOnClickListener {
            confirm("让雷达停转？停转后就没有点云了。") { sendCmd("停转") { s -> s.stopMotor() } }
        }
    }

    private fun seek(id: Int, min: Int, max: Int, onChange: (Int) -> Unit) {
        val sb = findViewById<SeekBar>(id)
        sb.max = max - min
        sb.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar?, progress: Int, fromUser: Boolean) {
                onChange(progress + min)
            }

            override fun onStartTrackingTouch(s: SeekBar?) {}
            override fun onStopTrackingTouch(s: SeekBar?) {}
        })
    }

    private fun preset(id: Int, rpm: Int) {
        findViewById<Button>(id).setOnClickListener {
            findViewById<SeekBar>(R.id.sbRpm).progress = (rpm - 300) / 30
            sendCmd("设置转速 $rpm") { it.setRpm(rpm) }
        }
    }

    private fun confirm(ask: String, ok: () -> Unit) {
        if (demo) {
            ok()
            return
        }
        AlertDialog.Builder(this)
            .setMessage(ask)
            .setPositiveButton("确定") { _, _ -> ok() }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun sendCmd(label: String, action: (RadarSource) -> Unit) {
        val s = src
        if (s == null) {
            tvAck.text = "$label：没连上，命令发不出去"
            return
        }
        if (!s.canCmd) {
            tvAck.text = "$label：当前数据源不支持控制"
            return
        }
        action(s)
        tvAck.text = "$label：已下发，等板卡应答…"
    }

    // ------------------------------------------------------------------ 连接
    private fun connect() {
        if (demo) disconnect()
        val host = etHost.text.toString().trim().ifEmpty { "192.168.9.1" }
        val port = etPort.text.toString().trim().toIntOrNull() ?: 46000
        if (host == "demo") {
            findViewById<Button>(R.id.btnDemo).performClick()
            return
        }
        prefs.edit().putString("host", host).putInt("port", port).apply()
        srcToken++
        val token = srcToken
        val c = T40Client(host, port, newListener(token))
        src = c
        demo = false
        linkMsg = "正在连接 $host:$port …"
        connected = false
        btnConnect.text = "断开"
        resetStats()
        c.start()
        updateBanner()
    }

    private fun disconnect() {
        srcToken++
        src?.stop()
        src = null
        pending = null
        demo = false
        connected = false
        linkMsg = "已断开"
        btnConnect.text = "连接"
        st = null
        resetStats()
        updateBanner()
    }

    private fun resetStats() {
        lastFrameMs = 0L
        lastStatusMs = 0L
        st = null
        prevBytes = -1L
        kbps = 0f
        rpm = 0f
        frameStamps.clear()
        ptsAccum = 0
        ptsAccumMs = 0
        fps = 0f
        pps = 0f
        ppi.resetCounters()
    }

    // ------------------------------------------------------------------ 回调（后台线程 → 主线程）
    /**
     * 每次连接生成一个带 token 的监听器：旧连接迟到的回调 token 对不上，直接丢。
     * 这是"断开后界面仍被旧连接钉住"的根治办法。
     */
    private fun newListener(token: Int) = object : T40Client.Listener {
        override fun onState(text: String, linkUp: Boolean) {
            ui.post { if (token == srcToken) applyLink(text, linkUp) }
        }

        override fun onFrame(f: RadarDecoder.Frame) {
            if (token != srcToken) return
            pending = f
            if (!drainScheduled) {
                drainScheduled = true
                ui.post(drain)
            }
        }

        override fun onStatus(s: T40Client.BoardStatus) {
            ui.post { if (token == srcToken) applyStatus(s) }
        }

        override fun onAck(op: Int, result: Int, msg: String) {
            ui.post { if (token == srcToken) applyAck(op, result, msg) }
        }

        override fun onLog(text: String) {
            // 板卡日志只进命令栏，绝不碰链路状态（里面有"CAN 打开失败"这类字眼）
            ui.post { if (token == srcToken) tvAck.text = text }
        }
    }

    private val drain = Runnable {
        drainScheduled = false
        val f = pending ?: return@Runnable
        pending = null
        handleFrame(f)
    }

    private fun applyLink(text: String, linkUp: Boolean) {
        linkMsg = text
        connected = linkUp
        updateBanner()
    }

    private fun applyStatus(s: T40Client.BoardStatus) {
        lastStatusMs = SystemClock.uptimeMillis()
        if (prevBytes >= 0 && s.atMs > prevBytesMs) {
            val d = s.radarBytes - prevBytes
            if (d >= 0) kbps = d / ((s.atMs - prevBytesMs) / 1000f) / 1024f
        }
        prevBytes = s.radarBytes
        prevBytesMs = s.atMs
        st = s
    }

    private fun applyAck(op: Int, result: Int, msg: String) {
        val name = when (op) {
            T40Client.OP_SET_RPM -> "设置转速"
            T40Client.OP_MOTOR_ENABLE -> "马达使能"
            T40Client.OP_MOTOR_DISABLE -> "马达停"
            T40Client.OP_STOP -> "停转"
            else -> "op0x%02X".format(op)
        }
        ackText = if (result == 0) "$name：成功" else "$name：失败(result=$result)"
        if (msg.isNotEmpty() && msg != "demo") ackText += "  $msg"
        tvAck.text = ackText
    }

    // ------------------------------------------------------------------ 帧处理 / 状态
    private fun handleFrame(f: RadarDecoder.Frame) {
        val now = SystemClock.uptimeMillis()
        lastFrameMs = now
        val kept = ppi.pushFrame(f)

        val rp = RadarDecoder.rpmFromC5(f.az, f.c5)
        if (rp > 0f) rpm = if (rpm == 0f) rp else rpm * 0.9f + rp * 0.1f

        frameStamps.addLast(now)
        // 点数按"过阈值之后"算，和 PC 端的点/秒同口径
        ptsAccum += kept
        if (ptsAccumMs == 0L) ptsAccumMs = now
    }

    private val ticker = object : Runnable {
        override fun run() {
            val now = SystemClock.uptimeMillis()
            while (frameStamps.isNotEmpty() && now - frameStamps.first() > 1000L) {
                frameStamps.removeFirst()
            }
            fps = frameStamps.size.toFloat()
            val span = (now - ptsAccumMs) / 1000f
            if (span > 0.5f) {
                pps = ptsAccum / span
                ptsAccum = 0
                ptsAccumMs = now
            }
            renderStats()
            updateBanner()
            ui.postDelayed(this, 500)
        }
    }

    private fun renderStats() {
        val s = st
        val sb = StringBuilder()
        val link = when {
            demo -> "演示"
            src == null -> "未连接"
            connected -> src?.name ?: "-"
            else -> "重连中…"
        }
        sb.append("链路  ").append(link).append('\n')
        sb.append("帧率  ").append("%.1f".format(fps)).append(" fps   点 ")
            .append("%.0f".format(pps)).append("/s\n")
        sb.append("本帧  ").append(ppi.ptsLast).append(" 点   累计 ").append(ppi.ptsTotal).append('\n')
        sb.append("c0 序号 ").append(if (ppi.lastSeq < 0) "-" else ppi.lastSeq)
            .append("   校验 ").append(if (ppi.lastSumOk) "OK" else "FAIL").append('\n')
        sb.append("转速  ").append(if (rpm <= 0f) "-" else "%.0f rpm（c5 反推）".format(rpm)).append('\n')
        if (s != null) {
            sb.append("板卡  pkts ").append(s.radarPkts)
                .append("   drop ").append(s.dropped)
                .append("   wifi ").append(s.wifi)
                .append("   rssi ").append(if (s.rssi < 0) "N/A" else s.rssi.toString())
                .append("   ").append("%.1f".format(kbps)).append(" KB/s")
                .append("   age ").append(s.statusAge).append(" s\n")
        }
        sb.append("命令  ").append(ackText)
        stats.text = sb
    }

    /** 状态判读：顺序和 PC 端 _watchdog 一样，从"最上游"往下排。 */
    private fun updateBanner() {
        val now = SystemClock.uptimeMillis()
        val s = st
        val level: Int
        val text: String
        if (!connected) {
            level = ERR
            text = if (src != null) {
                // 还在自动重连：别再让人点「连接」（那会把重连掐掉）
                "$linkMsg\n客户端会自动重连，不用点「连接」"
            } else {
                "$linkMsg\n先让手机连上板卡的 WiFi（t40_link），再点「连接」"
            }
        } else if (demo) {
            level = OK
            text = "演示数据（没有硬件）  %.1f fps   %.0f 点/s".format(fps, pps)
        } else if (s == null || now - lastStatusMs > 5000L) {
            level = ERR
            text = "已连板卡，但收不到 STATUS —— 板卡侧桥接卡住（典型是 ath6kl 楔死）。断电重启板卡。"
        } else if (s.statusAge > 10) {
            level = ERR
            text = "板卡侧采集卡住 ${s.statusAge} s（STATUS 还在发，读网卡/CAN 的调用不返回）。"
        } else if (s.wifi == 0) {
            level = WARN
            text = "板卡看不到雷达（wifi=0）：雷达没上电 / 没关联上板卡的 radar_wifi。"
        } else if (lastFrameMs == 0L) {
            level = WARN
            text = "已连板卡（STATUS 正常），等待第一帧 0x00F0 …"
        } else if (now - lastFrameMs > 3000L) {
            level = WARN
            text = "已 ${(now - lastFrameMs) / 1000} s 没有新点云：雷达停转，或板卡→手机这段 WiFi 在丢大包。"
        } else {
            level = OK
            text = "正常  %.1f fps   %.0f 点/s".format(fps, pps)
        }
        val color = when (level) {
            OK -> 0xFF1B4D2E.toInt()
            WARN -> 0xFF6B5320.toInt()
            else -> 0xFF7A1F1F.toInt()
        }
        if (text != lastBanner || color != lastBannerColor) {
            lastBanner = text
            lastBannerColor = color
            banner.text = text
            banner.setBackgroundColor(color)
        }
    }

    // ------------------------------------------------------------------ 生命周期
    override fun onDestroy() {
        super.onDestroy()
        ui.removeCallbacksAndMessages(null)
        src?.stop()
        src = null
    }

    private fun immersive() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
    }

    companion object {
        private const val OK = 0
        private const val WARN = 1
        private const val ERR = 2
    }
}
