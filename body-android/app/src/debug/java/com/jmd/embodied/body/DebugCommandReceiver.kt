package com.jmd.embodied.body

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Debug builds only (src/debug): test commands over adb, without touching the phone.
 * Callers need android.permission.DUMP (signature|privileged|development): the adb shell holds it, as can
 * preinstalled privileged apps and any app that declares it and is granted it with `adb shell pm grant`;
 * ordinary installed apps cannot obtain it on their own. Release builds do not contain this class.
 * On-screen pairing must replace this before the microphone streams (P2-02; protocol/v0.md section 10).
 *
 *   PAIR: `python -m brain pair` (the token goes to adb on stdin, never on a laptop command line)
 *   ... -a com.jmd.embodied.body.debug.CONNECT [--es host 127.0.0.1 --ei port 8765] [--ei unreachable_s 30]
 *   ... -a com.jmd.embodied.body.debug.ECHO [--ei count 1000 --ei bytes 102400]
 *   ... -a com.jmd.embodied.body.debug.RECONNECT
 *   ... -a com.jmd.embodied.body.debug.STOP
 *   ... -a com.jmd.embodied.body.debug.UNPAIR
 */
class DebugCommandReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val ctx = context.applicationContext
        val action = intent.action?.removePrefix(PREFIX) ?: return
        val extras = intent.extras
        // Clears only this process's copy of the intent (the system's broadcast record is not affected);
        // it keeps the token from lingering in our own memory longer than needed.
        intent.replaceExtras(null as android.os.Bundle?)
        when (action) {
            "PAIR" -> {
                val token = LinkAuth.parseToken(extras?.getString("token"))
                if (token == null) {
                    resultData = "bad token"
                    Log.w(BrainLink.TAG, "debug: pairing refused (token must be 64 hex characters)")
                } else {
                    LinkSettings.saveToken(ctx, token)
                    resultData = "paired"
                    Log.i(BrainLink.TAG, "debug: paired with a brain token")
                }
            }
            "UNPAIR" -> {
                LinkSettings.clearToken(ctx)
                LinkService.stop(ctx)
                resultData = "unpaired"
            }
            "CONNECT" -> {
                val (oldHost, oldPort) = LinkSettings.load(ctx)
                val host = extras?.getString("host") ?: oldHost
                val port = extras?.getInt("port", oldPort) ?: oldPort
                if (LinkSettings.parse("$host:$port") == null) {
                    resultData = "bad host:port"
                    return
                }
                LinkSettings.save(ctx, host, port)
                extras?.getInt("unreachable_s", 0)?.takeIf { it > 0 }?.let { BrainLink.setUnreachableStopMs(ctx, it * 1000L) }
                try {
                    LinkService.connect(ctx)
                    resultData = "connecting"
                } catch (e: Exception) {  // e.g. ForegroundServiceStartNotAllowedException from the background
                    Log.w(BrainLink.TAG, "debug: could not start the link service: ${e.javaClass.simpleName}: ${e.message}")
                    resultData = "fgs refused: ${e.javaClass.simpleName}"
                }
            }
            "ECHO" -> {
                val count = (extras?.getInt("count", 1000) ?: 1000).coerceIn(1, BrainLink.MAX_ECHO_COUNT)
                val bytes = (extras?.getInt("bytes", 100 * 1024) ?: (100 * 1024)).coerceIn(0, BrainLink.MAX_ECHO_BYTES)
                BrainLink.runEcho(count, bytes)
                resultData = "echo $count x $bytes"
            }
            "RECONNECT" -> { BrainLink.reconnect(); resultData = "reconnect" }
            "STOP" -> { LinkService.stop(ctx); resultData = "stopping" }
            else -> Log.w(BrainLink.TAG, "debug: unknown action ignored")
        }
    }

    companion object {
        const val PREFIX = "com.jmd.embodied.body.debug."
    }
}
