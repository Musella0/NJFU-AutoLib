# AutoLib Android App

AutoLib 的安卓 WebView 壳，打包后可安装到手机，点图标直接进入你的 AutoLib 服务器界面。

---

## 快速开始

### 前置要求

| 工具 | 版本 | 说明 |
|---|---|---|
| JDK | 17+ | `java -version` 确认 |
| Android SDK | API 34 | 通过 Android Studio 安装 |
| `ANDROID_HOME` | 已设置 | 指向 SDK 目录 |

最简单的方式是安装 [Android Studio](https://developer.android.com/studio)，它会自动配置好 SDK 和环境变量。

---

## 第一步：修改服务器地址

打开 `app/build.gradle.kts`，找到这一行：

```kotlin
buildConfigField("String", "SERVER_URL", "\"https://example.com\"")
```

把 `https://example.com` 换成你的服务器地址，例如：

```kotlin
buildConfigField("String", "SERVER_URL", "\"https://autolib.yourdomain.com\"")
```

---

## 第二步：获取 Gradle Wrapper JAR

由于 `gradle-wrapper.jar` 是二进制文件不适合放进 git，首次使用需要下载：

```bash
cd android

# 方案 A：如果已有 Gradle（推荐）
gradle wrapper --gradle-version=8.7

# 方案 B：用 Android Studio 打开项目，IDE 会自动下载
```

---

## 第三步：打包调试版 APK

```bash
cd android
./gradlew assembleDebug
```

APK 输出在：`app/build/outputs/apk/debug/app-debug.apk`

安装到手机（需要 adb 并开启 USB 调试）：

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

---

## 打包正式版（Release）

### 1. 生成签名 Keystore

```bash
keytool -genkeypair -v \
  -keystore autolib-release.jks \
  -keyalg RSA -keysize 2048 \
  -validity 10000 \
  -alias autolib
```

### 2. 在 `app/build.gradle.kts` 加入签名配置

```kotlin
signingConfigs {
    create("release") {
        storeFile = file("../../autolib-release.jks")
        storePassword = "你的密码"
        keyAlias = "autolib"
        keyPassword = "你的密码"
    }
}
buildTypes {
    release {
        signingConfig = signingConfigs.getByName("release")
        // ... 其余保持不变
    }
}
```

### 3. 打包

```bash
./gradlew assembleRelease
```

APK 在：`app/build/outputs/apk/release/app-release.apk`

---

## 换图标

用 Android Studio 的 **Image Asset** 工具：

1. 右键 `app/src/main/res` → New → Image Asset
2. Icon Type 选 Launcher Icons (Adaptive and Legacy)
3. 导入你的图片（1024×1024 PNG 最佳）
4. 点 Finish，自动覆盖所有分辨率

---

## 常见问题

**Q: 打开 app 显示「无法连接到服务器」**  
A: 确认服务器已运行且域名可从手机访问。注意如果服务器用 HTTP（非 HTTPS），需要在 `AndroidManifest.xml` 把 `usesCleartextTraffic` 改为 `true`。

**Q: 登录后重启 app 需要重新登录**  
A: Cookie 应该是自动持久化的。如果没有，确认服务器的 `SESSION_COOKIE_SECURE=true`（使用 HTTPS 时必须）。

**Q: 返回键直接退出了 app**  
A: 正常行为——当 WebView 没有可以返回的历史页面时才会退出。
