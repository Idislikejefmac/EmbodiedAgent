package com.jmd.embodied.body

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.util.Locale

/**
 * Android TextToSpeech behind the [SpeechController] (Speech.kt, which holds the rules and is unit-tested).
 *
 * Everything here runs on the main thread: the link thread only posts to it, so audio can never block the link,
 * and TextToSpeech's own callbacks (binder threads) are posted back to it. The engine is created lazily, when
 * the link starts (or for the debug SPEAK command), and released when the link stops, with a bounded wait for
 * it to initialise. The audio usage is voice communication (plan P2-01) with transient-may-duck audio focus
 * while speaking.
 */
object AndroidSpeech {
    private const val INIT_TIMEOUT_MS = 10_000L
    private const val TICK_MS = 500L

    private val main = Handler(Looper.getMainLooper())
    private val core = SpeechController({ Log.i(BrainLink.TAG, it) })

    /** True while the engine is initialised and usable; read from the link thread for the hello capabilities. */
    @Volatile var usable = false
        private set

    /** True while the engine is still starting (the link waits a moment for it before its hello). */
    @Volatile var initPending = false
        private set

    /** Told (on the main thread) when [usable] changes; the link uses it to offer speak_local late. */
    @Volatile var onUsableChanged: ((Boolean) -> Unit)? = null

    private var appContext: Context? = null
    private var linkOwned = false
    private var testOwned = false
    private var pendingTest: String? = null
    private var tts: TextToSpeech? = null
    private var generation = 0
    private var initStartedMs = 0L
    private var ticking = false
    private var testCounter = 0

    // ---- called from any thread: each only posts ----

    /** The link started: initialise the engine (a no-op if it already is). */
    fun acquire(context: Context) {
        val ctx = context.applicationContext
        main.post { guarded("acquire") { linkOwned = true; ensureInit(ctx) } }
    }

    /** The link stopped: release the engine. */
    fun release() {
        main.post { guarded("release") { linkOwned = false; testOwned = false; pendingTest = null; shutdown() } }
    }

    /** A validated request from the brain. [reply] is called on the main thread; it must only post elsewhere. */
    fun submit(request: SpeakRequest, reply: SpeechReply) {
        main.post {
            try {
                core.submit(request, reply, SystemClock.elapsedRealtime())
                scheduleTick()
            } catch (t: Throwable) {
                Log.e(BrainLink.TAG, "speech submit failed", t)
                try {
                    reply.refuse("internal", "speech failed")
                } catch (t2: Throwable) {
                    Log.e(BrainLink.TAG, "speech reply failed", t2)
                }
            }
        }
    }

    /** Debug SPEAK: say [text] locally, no brain needed; releases the engine afterwards if no link is running. */
    fun speakTest(context: Context, text: String) {
        val ctx = context.applicationContext
        main.post {
            guarded("speakTest") {
                testOwned = true
                ensureInit(ctx)
                if (core.status == SpeechController.Status.READY) submitTest(text) else pendingTest = text
                scheduleTick()
            }
        }
    }

    // ---- main thread only ----

    private fun submitTest(text: String) {
        val parsed = SpeakRequest.parse("test-${++testCounter}", text, false)
        if (parsed !is SpeakRequest.Parsed.Ok) {
            Log.w(BrainLink.TAG, "debug speak refused the test text")
            return
        }
        core.submit(parsed.request, object : SpeechReply {
            override fun ack() {}
            override fun refuse(code: String, message: String) {
                Log.w(BrainLink.TAG, "debug speak refused ($code): $message")
            }
        }, SystemClock.elapsedRealtime())
    }

    private fun ensureInit(ctx: Context) {
        appContext = ctx
        if (core.status == SpeechController.Status.READY || core.status == SpeechController.Status.INITIALISING) return
        core.beginInit()
        initPending = true
        initStartedMs = SystemClock.elapsedRealtime()
        val g = ++generation
        val holder = arrayOfNulls<TextToSpeech>(1)
        try {
            holder[0] = TextToSpeech(ctx) { status -> main.post { guarded("init") { onInit(g, holder[0], status) } } }
            tts = holder[0]
        } catch (t: Throwable) {  // e.g. SecurityException from a locked-down engine
            Log.w(BrainLink.TAG, "TextToSpeech could not be created: ${t.javaClass.simpleName}")
            failInit(g, "TextToSpeech could not be created (${t.javaClass.simpleName})")
        }
        scheduleTick()
    }

    private fun onInit(g: Int, t: TextToSpeech?, status: Int) {
        if (g != generation || t == null) {
            t?.shutdown()
            return
        }
        if (status != TextToSpeech.SUCCESS) {
            failInit(g, "TextToSpeech init failed (status $status)")
            return
        }
        val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
        t.setAudioAttributes(attrs)
        val language = pickLanguage(t)
        if (language == null) {
            failInit(g, "no voice data for ${Locale.getDefault().toLanguageTag()} or English")
            return
        }
        t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = post { core.onStart(idOf(utteranceId), SystemClock.elapsedRealtime()) }
            override fun onDone(utteranceId: String?) = post { core.onDone(idOf(utteranceId), SystemClock.elapsedRealtime()) }
            override fun onStop(utteranceId: String?, interrupted: Boolean) =
                post { core.onError(idOf(utteranceId), -1, SystemClock.elapsedRealtime()) }

            @Deprecated("Deprecated in the framework", ReplaceWith("onError(utteranceId, errorCode)"))
            override fun onError(utteranceId: String?) = post { core.onError(idOf(utteranceId), -2, SystemClock.elapsedRealtime()) }
            override fun onError(utteranceId: String?, errorCode: Int) =
                post { core.onError(idOf(utteranceId), errorCode, SystemClock.elapsedRealtime()) }

            private fun idOf(id: String?) = id ?: ""
            private fun post(block: () -> Unit) {
                main.post { guarded("tts callback") { if (g == generation) block() } }
            }
        })
        val audio = appContext?.getSystemService(AudioManager::class.java)
        core.initFinished(AndroidEngine(t, attrs, audio), "")
        initPending = false
        Log.i(BrainLink.TAG, "TextToSpeech initialised (language $language)")
        setUsable(true)
        pendingTest?.let { pendingTest = null; submitTest(it) }
        scheduleTick()
    }

    /** The device language if the engine has its data, otherwise English; null if neither works. */
    private fun pickLanguage(t: TextToSpeech): String? {
        for (locale in listOf(Locale.getDefault(), Locale.US)) {
            val r = try {
                t.setLanguage(locale)
            } catch (e: RuntimeException) {
                TextToSpeech.LANG_NOT_SUPPORTED
            }
            if (r >= TextToSpeech.LANG_AVAILABLE) return locale.toLanguageTag()
            Log.w(BrainLink.TAG, "TextToSpeech language ${locale.toLanguageTag()} unusable (code $r)")
        }
        return null
    }

    private fun failInit(g: Int, why: String) {
        if (g != generation) return
        tts?.let { try { it.shutdown() } catch (e: RuntimeException) { Log.w(BrainLink.TAG, "TextToSpeech shutdown failed") } }
        tts = null
        core.initFinished(null, why)
        initPending = false
        setUsable(false)
        pendingTest = null
    }

    private fun shutdown() {
        generation++  // any init or callback still in flight belongs to a released engine
        core.close()
        tts?.let { try { it.shutdown() } catch (e: RuntimeException) { Log.w(BrainLink.TAG, "TextToSpeech shutdown failed") } }
        tts = null
        initPending = false
        setUsable(false)
        Log.i(BrainLink.TAG, "speech released")
    }

    private fun setUsable(now: Boolean) {
        if (usable == now) return
        usable = now
        onUsableChanged?.let { cb -> try { cb(now) } catch (t: Throwable) { Log.e(BrainLink.TAG, "speech listener failed", t) } }
    }

    private fun scheduleTick() {
        if (ticking) return
        ticking = true
        main.postDelayed({ tick() }, TICK_MS)
    }

    private fun tick() {
        ticking = false
        guarded("tick") {
            val now = SystemClock.elapsedRealtime()
            if (initPending && now - initStartedMs > INIT_TIMEOUT_MS) failInit(generation, "TextToSpeech init timed out")
            core.tick(now)
            if (!core.busy && !initPending && testOwned && !linkOwned) {  // a debug test finished with no link running
                testOwned = false
                shutdown()
            }
            if (core.busy || initPending) scheduleTick()
        }
    }

    private inline fun guarded(what: String, block: () -> Unit) {
        try {
            block()
        } catch (t: Throwable) {
            Log.e(BrainLink.TAG, "speech $what failed", t)
        }
    }

    private class AndroidEngine(
        private val tts: TextToSpeech,
        private val attrs: AudioAttributes,
        private val audio: AudioManager?,
    ) : SpeechEngine {
        private var focus: AudioFocusRequest? = null

        override fun speak(engineId: String, text: String, flush: Boolean): Boolean =
            tts.speak(text, if (flush) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, engineId) ==
                TextToSpeech.SUCCESS

        override fun stop() {
            tts.stop()
        }

        override fun shutdown() {
            tts.shutdown()
        }

        override fun setActive(active: Boolean) {
            val am = audio ?: return
            if (active) {
                if (focus != null) return
                val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(attrs).build()
                val r = am.requestAudioFocus(req)
                focus = req
                if (r != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) Log.w(BrainLink.TAG, "audio focus not granted (code $r); speaking anyway")
            } else {
                focus?.let { am.abandonAudioFocusRequest(it) }
                focus = null
            }
        }
    }
}
