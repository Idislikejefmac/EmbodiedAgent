package com.jmd.embodied.body

import android.os.SystemClock
import org.json.JSONObject
import java.util.zip.CRC32

/** Protocol v0 constants and envelope checks for the body side. Spec: protocol/v0.md. */
object Protocol {
    const val VERSION = 0
    const val MAX_TEXT_BYTES = 65536
    const val MAX_TS = 9007199254740991L  // 2^53 - 1

    val ENVELOPE = setOf("v", "type", "id", "reply_to", "ts_body", "ts_brain", "payload")

    val TYPES = setOf(
        "hello", "auth", "heartbeat", "pose", "tracking_state", "image", "audio_in",
        "audio_out", "speak_local", "event", "command", "ack", "error", "echo",
    )

    /** Types the brain may send to a body (section 4). Anything else gets wrong_direction. */
    val FROM_BRAIN = setOf("hello", "auth", "heartbeat", "audio_out", "speak_local", "command", "ack", "error", "echo")

    /** Error codes (section 9); a test checks this list and every code used in the Kotlin sources. */
    val ERROR_CODES = listOf(
        "bad_json", "bad_envelope", "bad_payload", "unsupported_version", "unknown_type",
        "wrong_direction", "bad_sequence", "bad_binary", "too_large", "unknown_command",
        "bad_args", "not_implemented", "busy", "internal", "unauthorized",
    )

    /** Close codes (section 1). */
    const val CLOSE_NORMAL = 1000
    const val CLOSE_GOING_AWAY = 1001
    const val CLOSE_PROTOCOL_ERROR = 1002
    const val CLOSE_AUTH_FAILED = 1008
    const val CLOSE_REPLACED = 4001

    /** The command allowlist (section 8). Default deny: anything else gets unknown_command. */
    val COMMANDS = listOf(
        "get_status", "capture_image", "start_audio_in", "stop_audio_in", "stop_audio_out", "set_pose_rate",
    )

    /** Allowlisted commands this body implements so far. */
    val IMPLEMENTED_COMMANDS = listOf("get_status")

    private val HEX256 = Regex("^[0-9a-f]{64}$")
    private val IDENT = Regex("^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")

    class ProtocolException(
        val code: String, message: String, val fatal: Boolean = false, val replyTo: Long? = null,
        /** True for a problem with a received `error`: logged, never answered (section 9). */
        val silent: Boolean = false,
    ) : Exception(message.replace(CONTROL, "?").take(500)) {
        init {
            require(code in ERROR_CODES) { "unknown error code $code" }
        }
    }

    private val CONTROL = Regex("[\\x00-\\x1f\\x7f]")

    /** A peer-supplied string made safe for one log line (quoted, control characters escaped). */
    fun quote(s: String?): String = JSONObject.quote((s ?: "").take(300))

    /** Body clock: elapsed realtime (counts through deep sleep), in microseconds. */
    fun nowUs(): Long = SystemClock.elapsedRealtimeNanos() / 1000

    fun crc32(data: ByteArray): Long = CRC32().apply { update(data) }.value

    /** UTF-8 length of [s] without allocating; the 64 KiB text cap counts bytes, not characters. */
    fun utf8Length(s: String): Int {
        var n = 0
        var i = 0
        while (i < s.length) {
            val c = s[i]
            n += when {
                c.code < 0x80 -> 1
                c.code < 0x800 -> 2
                Character.isHighSurrogate(c) && i + 1 < s.length && Character.isLowSurrogate(s[i + 1]) -> { i++; 4 }
                else -> 3
            }
            i++
        }
        return n
    }

    fun textTooLarge(s: String): Boolean = s.length > MAX_TEXT_BYTES || utf8Length(s) > MAX_TEXT_BYTES

    fun isHex256(x: Any?): Boolean = x is String && HEX256.matches(x)

    fun isIdent(x: Any?): Boolean = x is String && IDENT.matches(x)

    fun envelope(type: String, id: Long, replyTo: Long?, tsBody: Long?, tsBrain: Long?, payload: JSONObject) =
        JSONObject().apply {
            put("v", VERSION)
            put("type", type)
            put("id", id)
            put("reply_to", replyTo ?: JSONObject.NULL)
            put("ts_body", tsBody ?: JSONObject.NULL)
            put("ts_brain", tsBrain ?: JSONObject.NULL)
            put("payload", payload)
        }

    fun optLong(o: JSONObject, key: String): Long? {
        val x = o.opt(key)
        return if (x is Int || x is Long) (x as Number).toLong() else null
    }

    private fun validId(x: Long?): Long? = if (x != null && x in 1..BinaryFrame.MAX_ID) x else null

    fun hasBinary(msg: JSONObject): Boolean =
        msg.getString("type") in BinaryFrame.TYPE_CODES &&
            (optLong(msg.getJSONObject("payload"), "byte_length") ?: 0L) > 0L

    /** Check a message from the brain. Throws ProtocolException with the right error code. */
    fun validateFromBrain(o: JSONObject) {
        val id = validId(optLong(o, "id"))
        val silent = o.opt("type") == "error"
        // Same order as the brain: version, then type, then the closed envelope.
        if (!o.has("v")) throw ProtocolException("bad_envelope", "missing field v", replyTo = id, silent = silent)
        if (optLong(o, "v") != VERSION.toLong()) {
            throw ProtocolException("unsupported_version", "v=${o.opt("v").toString().take(32)}; this body speaks v0",
                fatal = true, replyTo = id, silent = silent)
        }
        val type = o.opt("type")
        if (type !is String || type !in TYPES) {
            throw ProtocolException("unknown_type", "unknown type ${type.toString().take(64)}", replyTo = id)
        }
        val unknown = o.keys().asSequence().filter { it !in ENVELOPE }.toList()
        if (unknown.isNotEmpty()) {
            throw ProtocolException("bad_envelope", "unknown envelope field ${unknown.first().take(64)}", replyTo = id,
                silent = silent)
        }
        val missing = ENVELOPE.filter { !o.has(it) }
        if (missing.isNotEmpty()) throw ProtocolException("bad_envelope", "missing field ${missing.first()}", replyTo = id, silent = silent)
        if (id == null) throw ProtocolException("bad_envelope", "bad id", silent = silent)
        if (!o.isNull("reply_to") && validId(optLong(o, "reply_to")) == null) {
            throw ProtocolException("bad_envelope", "reply_to is not an id or null", replyTo = id, silent = silent)
        }
        for (k in listOf("ts_body", "ts_brain")) {
            if (!o.isNull(k) && optLong(o, k)?.let { it in 0..MAX_TS } != true) {
                throw ProtocolException("bad_envelope", "$k is not a timestamp or null", replyTo = id, silent = silent)
            }
        }
        val payload = o.opt("payload") as? JSONObject
            ?: throw ProtocolException("bad_envelope", "payload is not an object", replyTo = id, silent = silent)
        if (type !in FROM_BRAIN) throw ProtocolException("wrong_direction", "$type may not be sent by the brain", replyTo = id)
        if (o.isNull("ts_brain")) {
            throw ProtocolException("bad_envelope", "ts_brain must be the brain's own timestamp", replyTo = id, silent = silent)
        }
        if (type == "ack" && o.isNull("reply_to")) throw ProtocolException("bad_envelope", "ack without reply_to", replyTo = id)
        when (type) {
            "command" -> validateCommand(payload, id)
            "hello" -> validateHello(payload, id)
            "auth" -> if (!isHex256(payload.opt("mac"))) {
                throw ProtocolException("bad_payload", "auth needs a mac", true, id)
            }
            "error" -> if (payload.opt("code") !is String || payload.opt("fatal") !is Boolean) {
                throw ProtocolException("bad_payload", "error needs code and fatal", replyTo = id, silent = true)
            }
        }
        if (type in BinaryFrame.TYPE_CODES) {
            val n = optLong(payload, "byte_length")
            if (n == null || n < 0 || n > BinaryFrame.MAX_DATA) throw ProtocolException("bad_payload", "bad byte_length", replyTo = id)
        }
        if (type == "echo") {
            if (optLong(payload, "seq") == null || payload.opt("text") !is String || optLong(payload, "crc32") == null) {
                throw ProtocolException("bad_payload", "echo needs seq, text, byte_length, crc32", replyTo = id)
            }
        }
    }

    private fun validateHello(payload: JSONObject, id: Long) {
        if (payload.opt("role") != "brain") throw ProtocolException("bad_payload", "hello.role must be brain", true, id)
        if (!isIdent(payload.opt("session_id"))) throw ProtocolException("bad_payload", "bad session_id", true, id)
        val auth = payload.optJSONObject("auth")
        if (auth == null || !isHex256(auth.opt("nonce"))) {
            throw ProtocolException("bad_payload", "brain hello carries no auth nonce", true, id)
        }
        val versions = payload.optJSONArray("versions")
        if (versions != null && (0 until versions.length()).none { versions.opt(it) == VERSION }) {
            throw ProtocolException("unsupported_version", "brain does not speak v0", true, id)
        }
    }

    private fun validateCommand(payload: JSONObject, id: Long) {
        val name = payload.opt("name")
        if (name !is String || name !in COMMANDS) {
            throw ProtocolException("unknown_command", "command '${name?.toString()?.take(64)}' is not in the v0 allowlist", replyTo = id)
        }
        val args = payload.opt("args") as? JSONObject
            ?: throw ProtocolException("bad_args", "args must be an object", replyTo = id)
        val keys = args.keys().asSequence().toSet()
        val ok = when (name) {
            "capture_image" -> keys == setOf("max_edge") && optLong(args, "max_edge")?.let { it in 64..4096 } == true
            "set_pose_rate" -> keys == setOf("hz") && (args.opt("hz") as? Number)?.toDouble()?.let { it in 0.0..30.0 } == true
            else -> keys.isEmpty()
        }
        if (!ok) throw ProtocolException("bad_args", "bad args for $name: ${keys.sorted().map { it.take(32) }}", replyTo = id)
    }
}
