package com.jmd.embodied.body

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import android.widget.TextView
import com.google.ar.core.ArCoreApk
import com.google.ar.core.Config
import com.google.ar.core.Session

// P0-02 checker screen: reports ARCore availability and Depth support for this phone.
// Moved out of MainActivity in P0-05; opened from the "ARCore check" button.
class ArCheckActivity : Activity() {
    private lateinit var view: TextView
    private val lines = mutableListOf("Embodied body: ARCore checker")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        view = TextView(this).apply {
            textSize = 18f
            setPadding(48, 48, 48, 48)
        }
        setContentView(view)
        render()
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            runCheck()
        } else {
            add("Camera permission needed to create an ARCore session; requesting.")
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 1)
        }
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        if (results.isNotEmpty() && results[0] == PackageManager.PERMISSION_GRANTED) {
            runCheck()
        } else {
            add("RESULT camera permission denied; cannot create session")
        }
    }

    private fun runCheck() {
        val availability = ArCoreApk.getInstance().checkAvailability(this)
        add("checkAvailability: $availability")
        try {
            val session = Session(this)
            add("RESULT session created: tracking supported")
            val depth = session.isDepthModeSupported(Config.DepthMode.AUTOMATIC)
            add("RESULT depth AUTOMATIC supported: $depth")
            session.close()
        } catch (e: Exception) {
            add("RESULT session failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun add(line: String) {
        Log.i("ArCheck", line)
        lines.add(line)
        render()
    }

    private fun render() {
        view.text = lines.joinToString("\n\n")
    }
}
