package com.jmd.embodied.body

import android.app.Activity
import android.os.Bundle
import android.widget.TextView

class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = TextView(this).apply {
            text = "Embodied body: hello v2"
            textSize = 28f
            setPadding(48, 48, 48, 48)
        }
        setContentView(text)
    }
}
