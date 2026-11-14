package com.jmd.embodied.body

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Protocol v0 binary frame (protocol/v0.md section 5). Pure JVM code so it can be unit tested.
 *
 * Header, 20 bytes, big-endian:
 * v uint8 | type uint8 | flags uint16 | id uint32 | ts int64 (microseconds) | length uint32, then data.
 */
class BinaryFrame(val type: String, val id: Long, val ts: Long, val data: ByteArray, val flags: Int = 0) {

    class FrameException(val code: String, message: String) : Exception(message)

    fun pack(): ByteArray {
        val code = TYPE_CODES[type] ?: throw IllegalArgumentException("type $type has no binary frame")
        require(id in 1..MAX_ID) { "id out of range" }
        require(ts in 0..Protocol.MAX_TS) { "ts out of range" }
        require(data.size <= MAX_DATA) { "data over 8 MiB" }
        val buf = ByteBuffer.allocate(HEADER_SIZE + data.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(VERSION.toByte())
        buf.put(code.toByte())
        buf.putShort(0)
        buf.putInt(id.toInt())          // low 32 bits; id <= 2^32-1
        buf.putLong(ts)
        buf.putInt(data.size)
        buf.put(data)
        return buf.array()
    }

    companion object {
        const val HEADER_SIZE = 20
        const val VERSION = 0
        const val MAX_DATA = 8 * 1024 * 1024
        const val MAX_ID = 4294967295L
        val TYPE_CODES = mapOf("image" to 1, "audio_in" to 2, "audio_out" to 3, "echo" to 4)
        private val TYPE_NAMES = TYPE_CODES.entries.associate { (k, v) -> v to k }

        @Throws(FrameException::class)
        fun unpack(frame: ByteArray): BinaryFrame {
            if (frame.size < HEADER_SIZE) {
                throw FrameException("bad_binary", "frame of ${frame.size} bytes is shorter than the header")
            }
            val buf = ByteBuffer.wrap(frame).order(ByteOrder.BIG_ENDIAN)
            val v = buf.get().toInt() and 0xFF
            val code = buf.get().toInt() and 0xFF
            val flags = buf.getShort().toInt() and 0xFFFF
            val id = buf.getInt().toLong() and 0xFFFFFFFFL
            val ts = buf.getLong()
            val length = buf.getInt().toLong() and 0xFFFFFFFFL
            if (v != VERSION) throw FrameException("unsupported_version", "binary frame v=$v")
            val type = TYPE_NAMES[code] ?: throw FrameException("bad_binary", "unknown binary type code $code")
            if (ts < 0) throw FrameException("bad_binary", "negative timestamp $ts")
            if (length > MAX_DATA) throw FrameException("too_large", "binary length $length over 8 MiB")
            if (length != (frame.size - HEADER_SIZE).toLong()) {
                throw FrameException("bad_binary", "header length $length but ${frame.size - HEADER_SIZE} data bytes")
            }
            return BinaryFrame(type, id, ts, frame.copyOfRange(HEADER_SIZE, frame.size), flags)
        }
    }
}
