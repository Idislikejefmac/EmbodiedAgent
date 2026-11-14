package com.jmd.embodied.body

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * P0-05 link screen: brain host:port setting, Connect and Stop, a link-state indicator, and the echo test.
 * The link starts only when Connect is pressed. Test hooks for adb live in the debug-only
 * DebugCommandReceiver (src/debug), not here.
 */
class MainActivity : Activity(), BrainLink.Listener {
    private lateinit var status: TextView
    private lateinit var target: EditText
    private lateinit var logView: TextView
    private lateinit var notice: TextView
    private val lines = ArrayDeque<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = 32
        status = TextView(this).apply { textSize = 28f; setPadding(0, 0, 0, pad) }
        target = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            hint = "brain host:port"
        }
        val connect = Button(this).apply { text = "Save and connect"; setOnClickListener { saveAndConnect() } }
        val stop = Button(this).apply { text = "Stop"; setOnClickListener { LinkService.stop(this@MainActivity) } }
        val echoButton = Button(this).apply {
            text = "Echo test (1,000 x 100 kB)"
            setOnClickListener { BrainLink.runEcho(1000, 100 * 1024) }
        }
        val arCheck = Button(this).apply {
            text = "ARCore check"
            setOnClickListener { startActivity(Intent(this@MainActivity, ArCheckActivity::class.java)) }
        }
        logView = TextView(this).apply { textSize = 13f; setPadding(0, pad, 0, 0) }
        notice = TextView(this).apply { textSize = 14f; setTextColor(Color.rgb(190, 30, 30)) }
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
            addView(TextView(this@MainActivity).apply { text = "Embodied body: brain link"; textSize = 20f })
            addView(status)
            addView(notice)
            addView(target)
            addView(connect)
            addView(stop)
            addView(echoButton)
            addView(arCheck)
            addView(logView)
        }
        setContentView(ScrollView(this).apply { addView(column) })

        val (host, port) = LinkSettings.load(this)
        target.setText("$host:$port")
        BrainLink.addListener(this)
        onState(BrainLink.state, BrainLink.detail)
        onEcho(BrainLink.echoRunning)
    }

    override fun onResume() {
        super.onResume()
        showNotificationNotice()
    }

    override fun onDestroy() {
        BrainLink.removeListener(this)
        super.onDestroy()
    }

    /** Without the notification permission the link still runs, but its notification (and Stop action) is
     * hidden; say so on screen, where the Stop button still works. */
    private fun showNotificationNotice() {
        val denied = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        notice.text = if (denied) "Notifications are off: while the link runs there is no status notification. " +
            "Use Stop here (or Settings > Apps > Embodied body > Force stop)." else ""
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        showNotificationNotice()
    }

    private fun saveAndConnect() {
        val parsed = LinkSettings.parse(target.text.toString())
        if (parsed == null) {
            onLog("not a host:port: ${target.text}")
            return
        }
        LinkSettings.save(this, parsed.first, parsed.second)
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)  // for the link notification
        }
        LinkService.connect(this)
    }

    override fun onState(state: BrainLink.State, detail: String) = runOnUiThread {
        val (label, color) = when (state) {
            BrainLink.State.CONNECTED -> "connected" to Color.rgb(0, 140, 60)
            BrainLink.State.CONNECTING -> "connecting..." to Color.rgb(200, 130, 0)
            BrainLink.State.WAITING -> "disconnected, retrying" to Color.rgb(190, 30, 30)
            BrainLink.State.NOT_PAIRED -> "not paired" to Color.rgb(150, 0, 150)
            BrainLink.State.STOPPED -> "stopped" to Color.GRAY
        }
        status.text = "$label\n$detail"
        status.setTextColor(color)
    }

    /** Keep the screen on only while an echo test runs. */
    override fun onEcho(running: Boolean) = runOnUiThread {
        if (running) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onLog(line: String) = runOnUiThread {
        lines.addFirst(line)
        while (lines.size > 20) lines.removeLast()
        logView.text = lines.joinToString("\n")
    }
}

/**
 * The brain address and the pairing token, in app-private SharedPreferences (allowBackup is off).
 * Default address 127.0.0.1:8765 (adb reverse). Cleartext ws:// is allowed only to 127.0.0.1 and localhost
 * (res/xml/network_security_config.xml), so a direct WiFi address needs that file changed and a rebuild.
 */
object LinkSettings {
    private const val PREFS = "link"
    private const val AUTH_PREFS = "link_auth"

    fun load(ctx: Context): Pair<String, Int> {
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return (p.getString("host", "127.0.0.1") ?: "127.0.0.1") to p.getInt("port", 8765)
    }

    fun save(ctx: Context, host: String, port: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("host", host).putInt("port", port).apply()
    }

    fun loadToken(ctx: Context): ByteArray? =
        LinkAuth.parseToken(ctx.getSharedPreferences(AUTH_PREFS, Context.MODE_PRIVATE).getString("token", null))

    fun saveToken(ctx: Context, token: ByteArray) {
        ctx.getSharedPreferences(AUTH_PREFS, Context.MODE_PRIVATE).edit().putString("token", LinkAuth.hex(token)).commit()
    }

    fun clearToken(ctx: Context) {
        ctx.getSharedPreferences(AUTH_PREFS, Context.MODE_PRIVATE).edit().remove("token").commit()
    }

    /** "host:port" with a plain hostname or IPv4 address and a port 1 to 65535. */
    fun parse(s: String): Pair<String, Int>? {
        val m = Regex("^([A-Za-z0-9.-]{1,253}):(\\d{1,5})$").matchEntire(s.trim()) ?: return null
        val port = m.groupValues[2].toInt()
        return if (port in 1..65535) m.groupValues[1] to port else null
    }
}
