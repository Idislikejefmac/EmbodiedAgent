package com.jmd.embodied.body

/**
 * Body-side backpressure (protocol/v0.md section 7, "Backpressure"). Pure JVM code so it can be unit tested.
 *
 * OkHttp closes the WebSocket when its outgoing queue passes 16 MiB, so bulk data must never be handed to it
 * blindly. Two classes of message:
 *  - critical (hello, auth, heartbeat, ack, error, event): small, written at once, never dropped;
 *  - bulk (image, audio_in, pose, echo): held here and handed to the socket only while the socket's own
 *    queue ([socketQueued], OkHttp's queueSize()) is under [socketCap]. While bulk waits here, at most
 *    [pendingCap] bytes are kept; when a new item would exceed that, the OLDEST bulk items are dropped first
 *    (stale sensor data is worth less than fresh).
 * An item larger than a cap is still allowed when nothing else is queued, so one 8 MiB image can pass.
 * Ids and timestamps are assigned by [Item.write] at the moment of writing, so dropped items leave no id gap.
 */
class SendQueue(
    private val socketQueued: () -> Long,
    val socketCap: Long = 2L * 1024 * 1024,
    val pendingCap: Long = 4L * 1024 * 1024,
) {
    class Item(val type: String, val bytes: Long, val onDrop: () -> Unit = {}, val write: () -> Boolean)

    private val pending = ArrayDeque<Item>()
    var pendingBytes = 0L
        private set
    var dropped = 0
        private set
    var droppedBytes = 0L
        private set
    val pendingCount: Int get() = pending.size

    /** Write a critical message now. Returns false if the socket refused it. */
    fun critical(item: Item): Boolean = item.write()

    /** Queue a bulk message, dropping the oldest queued bulk if needed, then try to drain. */
    fun offer(item: Item, onDrop: (Item) -> Unit = {}): Boolean {
        while (pending.isNotEmpty() && pendingBytes + item.bytes > pendingCap) {
            val old = pending.removeFirst()
            pendingBytes -= old.bytes
            dropped++
            droppedBytes += old.bytes
            old.onDrop()
            onDrop(old)
        }
        pending.addLast(item)
        pendingBytes += item.bytes
        return pump()
    }

    /** Hand queued bulk to the socket while it has room. Returns false if the socket refused a write. */
    fun pump(): Boolean {
        while (pending.isNotEmpty()) {
            val next = pending.first()
            val queued = socketQueued()
            if (queued > 0 && queued + next.bytes > socketCap) return true
            pending.removeFirst()
            pendingBytes -= next.bytes
            if (!next.write()) return false
        }
        return true
    }

    /** Forget queued bulk (connection gone). Returns the items dropped. */
    fun clear(): List<Item> {
        val all = pending.toList()
        pending.clear()
        pendingBytes = 0
        return all
    }

    companion object {
        val CRITICAL = setOf("hello", "auth", "heartbeat", "ack", "error", "event")
    }
}
