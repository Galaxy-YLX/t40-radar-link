plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.t40.radar"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.t40.radar"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // 自用工具，不上应用商店：release 也用 debug 签名，只为装得上、跑得快
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// 这个 App 只依赖平台 API，不引 AndroidX / 第三方库：
// 装得上、包小、断网也能编（依赖解析不需要拉一堆 AAR）。
dependencies {
    testImplementation("junit:junit:4.13.2")
}
