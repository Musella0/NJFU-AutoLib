# AutoLib Android 原生客户端

这是 AutoLib 的原生 Android 客户端。界面由 Android View / Material Components 构成，直接调用后端 `/api/**` JSON 接口，不加载网页或 HTML。

## 已实现

- 原生主页：今日/明日预约、预约状态（含已结束/已违约/已到馆）、调整明日、本周配置预览、公告和预约结果
- 原生预约：选择区域、座位和时段后立即预约，支持常用座位快捷选择
- 按图选座：12 张楼层平面图上拖动缩放选座，点哪儿选中离它最近的座位，并标出自己的优先级顺序和「还有几个人也选了它」；添加候选座位和立即预约都能进
- 原生预约操作：到馆、取消、一键午休续约
- 原生配置：按星期/统一时段、一天多时段、候选座位优先级（拖拽排序）、自动预约、迟到保护和自动午休
- 凭据验证：验证统一身份认证与图书馆 CAS 后保存
- 原生账号：添加学号即登录、资料编辑、退出以及多个图书馆学号切换
- 原生设置：主题（跟随系统/亮色/暗色）、邮箱通知（含「仅异常 / 全部」通知范围）、午休配置、学习记录、功能说明
- Flask 会话 Cookie 本地持久化

## 登录模型

与网页端一致，**没有独立的登录/注册入口**。用户在「添加学号」里填写学号和统一
身份认证（网上办事大厅）密码，后端 `/api/my/accounts/<pid>/verify` 验证通过后
会把当前会话提升为该学号的登录态，配置随学号存在云端，换设备重新验证一次即可
找回。未添加学号前是只读的游客状态。

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
