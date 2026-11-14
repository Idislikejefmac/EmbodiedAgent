package com.jmd.embodied.body

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Shared hostile corpus (protocol/corpus/v0-hostile.json, written by tools/gen_hostile_corpus.py).
 * Every verdict in it comes from running the Python validator. This test replays the cases the phone would
 * see (sent by the brain, or garbage with no sender) through the same path as BrainLink.onText/onBinary
 * and asserts: (a) nothing is ever thrown except the parser's own rejection types, and (b) the accept/reject
 * verdict equals Python's. Where Kotlin and Python disagree the case id is listed below with the reason;
 * the test asserts that each listed divergence STILL occurs, so whoever fixes the Kotlin side gets a failure
 * telling them to delete the entry. Never edit an expectation to make a divergence disappear.
 */
class HostileCorpusTest {
    private class Outcome(val kind: String, val code: String? = null, val detail: String = "") {
        val verdict get() = if (kind == "accept") "accept" else if (kind == "reject") "reject" else "throw"
        override fun toString() = if (code != null) "$kind($code)" else if (detail.isNotEmpty()) "$kind[$detail]" else kind
    }

    private val corpus: JSONObject by lazy {
        val f = File("../../protocol/corpus/v0-hostile.json")
        assertTrue("corpus not found at ${f.absolutePath}; run tools/gen_hostile_corpus.py", f.exists())
        JSONObject(f.readText(Charsets.US_ASCII))
    }

    private fun cases(key: String): List<JSONObject> {
        val a: JSONArray = corpus.getJSONArray(key)
        return (0 until a.length()).map { a.getJSONObject(it) }
    }

    /** What BrainLink.onText does with a frame from the brain: size cap, parse, validateFromBrain, hasBinary. */
    private fun runText(text: String): Outcome = try {
        try {
            if (Protocol.textTooLarge(text)) throw Protocol.ProtocolException("too_large", "text frame too large", true)
            val msg = try {
                JSONObject(text)
            } catch (e: Exception) {
                throw Protocol.ProtocolException("bad_json", "not a JSON object")
            } catch (e: StackOverflowError) {
                throw Protocol.ProtocolException("bad_json", "nested too deeply")
            }
            Protocol.validateFromBrain(msg)
            Protocol.hasBinary(msg)
            Outcome("accept")
        } catch (e: Protocol.ProtocolException) {
            Outcome("reject", e.code)
        }
    } catch (t: Throwable) {
        Outcome("throw", detail = t.javaClass.simpleName + ": " + (t.message ?: "").take(60).replace('\n', ' '))
    }

    /** What BrainLink.onBinary does: BinaryFrame.unpack. */
    private fun runBinary(hex: String): Outcome = try {
        BinaryFrame.unpack(ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() })
        Outcome("accept")
    } catch (e: BinaryFrame.FrameException) {
        Outcome("reject", e.code)
    } catch (t: Throwable) {
        Outcome("throw", detail = t.javaClass.simpleName + ": " + (t.message ?: "").take(60).replace('\n', ' '))
    }

    private fun run(c: JSONObject): Outcome =
        if (c.getString("kind") == "binary") runBinary(c.getString("input")) else runText(c.getString("input"))

    /** Cases the phone never validates: text sent by the body (the brain validates those). */
    private fun forKotlin(c: JSONObject) = c.getString("kind") == "binary" || c.isNull("sender") || c.getString("sender") == "brain"

    // ---------------------------------------------------------------------------------------------------
    // Known divergences from Python (strict expected failures), grouped by root cause.
    // ---------------------------------------------------------------------------------------------------

    /** case id -> reason. Python verdict is in the corpus; the Kotlin verdict differs. */
    private val knownDivergences: Map<String, String> = mapOf<String, String>(
        // 1. org.json is a lenient parser; Python (strict json + no NaN/Infinity/overflow) refuses these as bad_json. KOTLIN LOOSER: the phone accepts non-JSON text from the brain.
        "lenient_hex_in_result" to "org.json accepts hex number 0x51; Python refuses it as bad_json",
        "lenient_inf_in_result" to "org.json accepts Infinity literal; Python refuses it as bad_json",
        "lenient_leading_zero_in_result" to "org.json accepts leading-zero number 081; Python refuses it as bad_json",
        "lenient_line_comment" to "org.json accepts // comment after the object; Python refuses it as bad_json",
        "lenient_nan_in_result" to "org.json accepts NaN literal; Python refuses it as bad_json",
        "lenient_null_byte_suffix" to "org.json accepts NUL after the object; Python refuses it as bad_json",
        "lenient_overflow_in_result" to "org.json accepts 1e400 (overflows to infinity); Python refuses it as bad_json",
        "lenient_plus_in_result" to "org.json accepts +81; Python refuses it as bad_json",
        "lenient_raw_tab_in_string" to "org.json accepts raw tab inside a string; Python refuses it as bad_json",
        "lenient_semicolon_sep" to "org.json accepts ; used as the key separator; Python refuses it as bad_json",
        "lenient_single_quoted_value" to "org.json accepts single-quoted string value; Python refuses it as bad_json",
        "lenient_single_quotes" to "org.json accepts single-quoted keys and values; Python refuses it as bad_json",
        "lenient_trailing_comma" to "org.json accepts trailing comma; Python refuses it as bad_json",
        "lenient_trailing_garbage" to "org.json accepts text after the object; Python refuses it as bad_json",
        "lenient_trailing_object" to "org.json accepts a second object after the first; Python refuses it as bad_json",
        "lenient_unquoted_keys" to "org.json accepts unquoted key; Python refuses it as bad_json",

        // 2. Duplicate object keys: org.json throws on a duplicate key (bad_json), Python json.loads is last-wins and accepts. KOTLIN STRICTER (the safer behaviour, but the two sides disagree on what a frame means; the spec should say which).
        "dup_key_payload" to "duplicate key: Kotlin rejects as bad_json, Python takes the last value and accepts",
        "dup_key_ts_brain" to "duplicate key: Kotlin rejects as bad_json, Python takes the last value and accepts",
        "dup_key_type_hb_then_command" to "duplicate key: Kotlin rejects as bad_json, Python takes the last value and accepts",
        "dup_key_v_bad_then_good" to "duplicate key: Kotlin rejects as bad_json, Python takes the last value and accepts",

        // 3. Binary header id and timestamp ranges: Kotlin only refuses a negative ts and never checks id; spec section 5 and Python want id 1..2^32-1 and ts 0..2^53-1. KOTLIN LOOSER.
        "bin_id_0" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",
        "bin_id_2p32_via_wrap" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",
        "bin_ts_4611686018427387904" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",
        "bin_ts_9007199254740992" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",
        "bin_ts_9007199254740993" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",
        "bin_ts_9223372036854775807" to "binary header: Kotlin accepts an id of 0 or a ts above 2^53-1 that Python refuses with bad_binary",

        // 4. Kotlin does not check payloads against the schema (only auth.mac, error.code/fatal, echo, command, hello role/session_id/nonce/versions). KOTLIN LOOSER: field types, ranges, control characters and lengths in ack, error, hello, audio_out, speak_local, echo that Python refuses as bad_payload reach the handlers.
        "ack_brain_payload_empty" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "ack_brain_payload_ok_result_list" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "ack_brain_payload_status_bad" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "ack_brain_payload_status_err_no_error" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "ack_brain_payload_status_none" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "echo_crc32_big" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "echo_crc32_neg" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "echo_crc32_u32" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "echo_seq_neg" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_message_int" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_message_int_no_reply" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_message_nl" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_message_nl_no_reply" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_no_message" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_no_message_no_reply" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_unknown_code" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_brain_unknown_code_no_reply" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "error_message_501_brain" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "hello_brain_versions_dupes" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "hello_brain_versions_null" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "hello_brain_versions_str" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_capabilities_true" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_heartbeat_ms_true" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_software_int" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_software_list" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_software_null" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_versions_int" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_wall_time_utc_int" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_wall_time_utc_list" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut01_hello_brain_payload_wall_time_utc_str" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_del_final" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_encoding_list" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_final_int" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_final_null" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_final_str" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_sample_rate_str" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_seq_obj" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_utterance_id_null" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_payload_utterance_id_obj" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut10_audio_out_brain_type_speak_local" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut11_speak_local_brain_payload_interrupt_null" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut11_speak_local_brain_payload_interrupt_str" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut11_speak_local_brain_payload_utterance_id_float" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "mut13_command_brain_type_speak_local" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "speak_frame_about_65536" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "speak_text_4000" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "uni_speak_text_bell" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "uni_speak_text_del" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "uni_speak_text_esc" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "uni_speak_text_newline" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",
        "uni_speak_text_nul" to "payload not schema-checked: Kotlin accepts a payload Python refuses with bad_payload",

        // 5. hello.versions and integer spelling: Kotlin is stricter than Python's schema.
        "hello_brain_versions_only1" to "hello.versions [1] (no 0): Kotlin refuses unsupported_version; Python's schema accepts it. KOTLIN STRICTER (arguably right: the brain does not speak v0)",
        "int_v_text_neg0" to "\"v\":-0 is the integer 0 in JSON: Python accepts it, org.json reads it as a double so Kotlin refuses unsupported_version. KOTLIN STRICTER (harmless)",
    )

    /** case id -> reason: Kotlin THROWS (not a documented rejection). Bigger finding than a verdict mismatch. */
    private val knownThrows: Map<String, String> = mapOf<String, String>(
        // validateFromBrain does `type.toString()` on the platform type Any! returned by JSONObject.opt; a MISSING "type" makes that call NPE
        // instead of throwing ProtocolException(unknown_type). The raw_* frames additionally reach it only because org.json is lenient (comments, single
        // quotes, trailing comma, trailing text, unquoted keys parse as an object without a type). BrainLink.onText catches only ProtocolException.
        "mut11_speak_local_brain_del_type" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_comment" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_single_quotes" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_trailing_comma" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_trailing_garbage" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_trailing_garbage_brain" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
        "raw_unquoted_key" to "NPE in validateFromBrain on a frame with no type (Python: reject)",
    )

    @Test
    fun corpusIsPresentAndBigEnough() {
        assertEquals(1, corpus.getInt("version"))
        assertTrue(cases("cases").size >= 400)
        assertTrue(cases("cases").map { it.getString("id") }.toSet().size == cases("cases").size)
    }

    @Test
    fun kotlinNeverThrowsAndAgreesWithPython() {
        val unexpectedThrows = mutableListOf<String>()
        val unexpectedDiffs = mutableListOf<String>()
        val staleDivergences = mutableListOf<String>()
        val staleThrows = mutableListOf<String>()
        val seen = mutableSetOf<String>()
        val codeDiffs = mutableListOf<String>()
        var compared = 0
        for (c in cases("cases").filter { forKotlin(it) }) {
            val id = c.getString("id")
            seen += id
            val py = c.getString("verdict")
            val pyCode = if (c.isNull("code")) null else c.getString("code")
            val k = run(c)
            compared++
            val throwsKnown = id in knownThrows
            val diffKnown = id in knownDivergences
            if (k.kind == "throw") {
                if (!throwsKnown) unexpectedThrows += "$id: $k (python $py/$pyCode)"
                continue
            }
            if (throwsKnown) staleThrows += "$id no longer throws ($k); delete it from knownThrows"
            val differs = k.verdict != py
            if (differs && !diffKnown) unexpectedDiffs += "$id: python $py/$pyCode, kotlin $k"
            if (!differs && diffKnown) staleDivergences += "$id now agrees with Python ($k); delete it from knownDivergences"
            if (!differs && k.kind == "reject" && k.code != pyCode) codeDiffs += "$id: python $pyCode, kotlin ${k.code}"
        }
        for (id in knownDivergences.keys + knownThrows.keys) assertTrue("known divergence $id is not in the corpus", id in seen)
        println("HostileCorpusTest: compared $compared cases; ${codeDiffs.size} reject-code differences (not asserted):")
        codeDiffs.forEach { println("  codediff $it") }
        val msg = buildString {
            if (unexpectedThrows.isNotEmpty()) append("\nKOTLIN THROWS:\n  " + unexpectedThrows.joinToString("\n  "))
            if (unexpectedDiffs.isNotEmpty()) append("\nUNEXPECTED DIVERGENCES:\n  " + unexpectedDiffs.joinToString("\n  "))
            if (staleDivergences.isNotEmpty()) append("\nFIXED (delete entry):\n  " + staleDivergences.joinToString("\n  "))
            if (staleThrows.isNotEmpty()) append("\nFIXED (delete entry):\n  " + staleThrows.joinToString("\n  "))
        }
        assertTrue(msg, msg.isEmpty())
    }

    // ---- link auth input shapes ----

    private fun authOutcome(c: JSONObject): Boolean {
        val input = c.getString("input")
        return when (c.getString("kind")) {
            "token" -> LinkAuth.parseToken(input) != null
            "hex256" -> Protocol.isHex256(input)
            "ident" -> Protocol.isIdent(input)
            "mac_check" -> {
                val ctx = corpus.getJSONObject("auth_context")
                val tok = LinkAuth.parseToken(ctx.getString("token"))!!
                val texts = ctx.getJSONArray("transcript_texts")
                val transcript = (0 until texts.length()).map { LinkAuth.digest(texts.getString(it)) }
                LinkAuth.check(tok, ctx.getString("role"), transcript, input)
            }
            else -> throw IllegalStateException("unknown auth kind")
        }
    }

    /** auth case id -> reason; Kotlin accepts or rejects differently from Python. */
    private val knownAuthDivergences: Map<String, String> = mapOf<String, String>(
        // Python str.strip() removes U+0085 (NEL) from a pasted token; Kotlin trim() does not. KOTLIN STRICTER (harmless: the token is
        // pasted by hand, and Kotlin refuses rather than mis-parses).
        "token_pad_nel" to "Python strip() removes U+0085, Kotlin trim() does not: Kotlin refuses a token Python accepts",
    )

    @Test
    fun linkAuthShapeChecksNeverThrowAndAgreeWithPython() {
        val failures = mutableListOf<String>()
        val seen = mutableSetOf<String>()
        for (c in cases("auth_cases")) {
            val id = c.getString("id")
            seen += id
            val py = c.getBoolean("python_ok")
            val k = try {
                authOutcome(c)
            } catch (t: Throwable) {
                failures += "$id: kotlin THREW ${t.javaClass.simpleName}: ${(t.message ?: "").take(60)}"
                continue
            }
            val known = id in knownAuthDivergences
            if (k != py && !known) failures += "$id (${c.getString("kind")}): python ok=$py, kotlin ok=$k"
            if (k == py && known) failures += "$id now agrees with Python; delete it from knownAuthDivergences"
        }
        for (id in knownAuthDivergences.keys) assertTrue("known auth divergence $id is not in the corpus", id in seen)
        assertTrue("\n" + failures.joinToString("\n"), failures.isEmpty())
    }
}
