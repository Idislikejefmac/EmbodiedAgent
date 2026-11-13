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
}

