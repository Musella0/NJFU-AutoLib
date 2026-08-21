import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

// 签名口令只放在本机的 local.properties（已被 .gitignore 排除），不进仓库。
// 缺失时跳过 signingConfig，release 仍可构建，只是产出未签名包。
val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}
val releaseStoreFile = localProps.getProperty("RELEASE_STORE_FILE")

android {
    namespace = "com.autolib.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.autolib.app"
        minSdk = 26
        targetSdk = 34
        // versionCode 只增不减：降回去会让已装用户无法覆盖安装，与展示用的 versionName 无关。
        // 升级检查也按它比大小，发版必须 +1，否则客户端认不出新版本。
        versionCode = 5
        versionName = "0.2.1"

        buildConfigField("String", "SERVER_URL", "\"https://xn--1jq43jfrduzxh22d.xn--fiqs8s\"")
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    signingConfigs {
        if (releaseStoreFile != null) {
            create("release") {
                storeFile = file(releaseStoreFile)
                storePassword = localProps.getProperty("RELEASE_STORE_PASSWORD")
                keyAlias = localProps.getProperty("RELEASE_KEY_ALIAS")
                keyPassword = localProps.getProperty("RELEASE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfigs.findByName("release")?.let { signingConfig = it }
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
