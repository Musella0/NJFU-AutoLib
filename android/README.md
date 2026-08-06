# AutoLib Android 原生客户端

这是 AutoLib 的原生 Android 客户端。界面由 Android View / Material Components 构成，直接调用后端 `/api/**` JSON 接口，不加载网页或 HTML。

## 已实现

- 原生主页：今日/明日预约、预约状态、公告和预约结果
- 原生预约：选择区域、座位和时段后立即预约
- 原生预约操作：到馆、取消、一键午休续约
- 原生配置：按星期/统一时段、候选座位优先级、自动预约和迟到保护
- 凭据验证：分别验证 WebVPN 与图书馆密码后保存
- 原生账号：登录、注册、资料编辑、退出以及多个图书馆学号切换
- 原生统计：本周/累计学习次数、时长和最近记录
- Flask 会话 Cookie 本地持久化

## 服务器地址

在 `app/build.gradle.kts` 的 `SERVER_URL` 中设置 HTTPS 后端地址：

```kotlin
buildConfigField("String", "SERVER_URL", "\"https://example.com\"")
```

后端必须部署本仓库 `backend/`，且客户端地址应能访问其 JSON API。

## 构建

项目需要 JDK 17 或更高版本、Android SDK 34 和 Gradle 8.x：

```powershell
cd android
.\gradlew.bat assembleDebug
```

调试 APK 输出到 `app/build/outputs/apk/debug/app-debug.apk`。发布包请配置自己的 signingConfig 后执行 `assembleRelease`，不要分发未签名的 release APK。

> 仓库若缺少 `gradle/wrapper/gradle-wrapper.jar`，可在安装了 Gradle 的环境中运行 `gradle wrapper --gradle-version 8.13` 补齐后再使用 `gradlew.bat`。
