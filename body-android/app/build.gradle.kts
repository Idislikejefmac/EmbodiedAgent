plugins {
    id("com.android.application")
}

android {
    namespace = "com.jmd.embodied.body"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.jmd.embodied.body"
        minSdk = 31
        targetSdk = 37
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("com.google.ar:core:1.57.0")
    // WebSocket client for protocol v0 (P0-05). 5.5.1 verified on Maven Central (released Oct 2026).
    implementation("com.squareup.okhttp3:okhttp:5.5.1")
    testImplementation("junit:junit:4.13.2")
    // A real org.json for unit tests (android.jar only has stubs), so Protocol.validateFromBrain is testable.
    testImplementation("org.json:json:20240303")
}

