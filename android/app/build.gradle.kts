plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.autolib.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.autolib.app"
        minSdk = 26
        targetSdk = 34
        // versionCode 只增不减：降回去会让已装用户无法覆盖安装，与展示用的 versionName 无关
        versionCode = 4
        versionName = "0.2"

        buildConfigField("String", "SERVER_URL", "\"https://xn--1jq43jfrduzxh22d.xn--fiqs8s\"")
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
    kotlinOptions {
        jvmTarget = "1.8"
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
}
