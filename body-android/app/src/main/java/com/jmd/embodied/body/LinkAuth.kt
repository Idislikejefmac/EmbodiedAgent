package com.jmd.embodied.body

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Link authentication (protocol/v0.md section 4.2), the same as brain/transport/auth.py.
 * Each side proves it holds the pairing token with HMAC-SHA256(token, label | sha256(frame) | ...) over the
 * exact handshake text frames so far, so the proof covers both nonces, the session id and every hello field.
 * The label differs per role so a proof cannot be reflected. The token never crosses the link and is never
 * logged. No session key and no per-message integrity: valid only with no third party on the path.
 * Pure JVM code so it can be unit tested.
 */
object LinkAuth {
    const val TOKEN_BYTES = 32
    val LABELS = mapOf("body" to "embodied-agent v0 body proof", "brain" to "embodied-agent v0 brain proof")
    private val random = SecureRandom()

    fun newNonce(): String = hex(ByteArray(32).also { random.nextBytes(it) })

    /** sha256 of one text frame exactly as sent or received (UTF-8), in hex. */
    fun digest(text: String): String = hex(MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8)))

    fun proof(token: ByteArray, role: String, transcript: List<String>): String {
        val label = LABELS[role] ?: throw IllegalArgumentException("role")
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token, "HmacSHA256"))
        return hex(mac.doFinal((listOf(label) + transcript).joinToString("|").toByteArray(Charsets.US_ASCII)))
    }

    /** Constant-time comparison of the peer's proof with the expected one. */
    fun check(token: ByteArray, role: String, transcript: List<String>, mac: String?): Boolean {
        if (mac == null) return false
        val want = proof(token, role, transcript).toByteArray(Charsets.US_ASCII)
        return MessageDigest.isEqual(want, mac.toByteArray(Charsets.US_ASCII))
    }

    /** 64 hex characters to 32 bytes, or null. */
    fun parseToken(text: String?): ByteArray? {
        val t = text?.trim()?.lowercase() ?: return null
        if (t.length != TOKEN_BYTES * 2 || t.any { it !in "0123456789abcdef" }) return null
        return ByteArray(TOKEN_BYTES) { t.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }

    fun hex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }
}
