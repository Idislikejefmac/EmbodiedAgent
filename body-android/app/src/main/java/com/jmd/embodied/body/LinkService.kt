package com.jmd.embodied.body

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Handler
import android.os.IBinder
import android.os.Looper

/**
 * Keeps the brain link alive while the app is not on screen (phone locked, or another app in front).
 *
 * Runs only between an explicit Connect and a stop: the Stop button, the notification's Stop action,
 * swiping the app away, or BrainLink stopping itself (3 minutes without a stable link, the brain shut down and
 * did not return within a minute, or another body replaced it; authentication failures are retried within those
 * 3 minutes, never an immediate stop). START_NOT_STICKY: Android never restarts it on its own.
 * The CPU wake lock lives in BrainLink and is held only while connected.
 */
class LinkService : Service(), BrainLink.Listener {
    private var foreground = false
    @Volatile private var destroyed = false  // a state posted after onDestroy must not re-post the notification
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(NotificationChannel(CHANNEL, "Brain link", NotificationManager.IMPORTANCE_LOW))
        BrainLink.addListener(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CONNECT -> {
                startForeground(NOTIFICATION_ID, notification("Connecting to the brain"),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
                foreground = true
                val (host, port) = LinkSettings.load(this)
                BrainLink.start(applicationContext, host, port)
            }
            ACTION_STOP -> {
                BrainLink.stop("stop requested")
                finish()
            }
            else -> finish()  // started without an explicit action: do nothing
        }
        return START_NOT_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        BrainLink.stop("app swiped away")
        finish()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        destroyed = true
        main.removeCallbacksAndMessages(null)
        BrainLink.removeListener(this)
        if (BrainLink.isRunning) BrainLink.stop("service destroyed")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    /** Called on the link thread: do the work on the main thread so nothing here can disturb the link. */
    override fun onState(state: BrainLink.State, detail: String) {
        main.post { applyState(state, detail) }
    }

    private fun applyState(state: BrainLink.State, detail: String) {
        if (destroyed) return
        val text = when (state) {
            BrainLink.State.CONNECTED -> "Connected to the brain ($detail)"
            BrainLink.State.CONNECTING -> "Connecting to the brain"
            BrainLink.State.WAITING -> "No link to the brain, retrying (gives up after 3 min)"
            BrainLink.State.NOT_PAIRED -> "Not paired"
            BrainLink.State.STOPPED -> "Stopped"
        }
        if (state == BrainLink.State.STOPPED || state == BrainLink.State.NOT_PAIRED) {
            finish()
        } else if (foreground) {
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
        }
    }

    override fun onLog(line: String) {}

    private fun finish() {
        if (foreground) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            foreground = false
        }
        stopSelf()
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, LinkService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Embodied body")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    companion object {
        private const val CHANNEL = "link"
        private const val NOTIFICATION_ID = 1
        const val ACTION_CONNECT = "com.jmd.embodied.body.action.CONNECT"
        const val ACTION_STOP = "com.jmd.embodied.body.action.STOP"

        fun connect(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, LinkService::class.java).setAction(ACTION_CONNECT))
        }

        /** Stop through the service (the same path as the notification's Stop action), or directly. */
        fun stop(ctx: Context) {
            if (!BrainLink.isRunning) {
                BrainLink.stop("stop requested")
                return
            }
            try {
                ctx.startService(Intent(ctx, LinkService::class.java).setAction(ACTION_STOP))
            } catch (e: IllegalStateException) {
                BrainLink.stop("stop requested")
            }
        }
    }
}
