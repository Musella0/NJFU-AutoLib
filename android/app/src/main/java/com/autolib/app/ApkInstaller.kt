package com.autolib.app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * App 内更新：把升级包下到私有缓存，再交给系统安装器。
 *
 * 原先是把下载地址丢给浏览器（ACTION_VIEW），用户得先在浏览器里点下载、
 * 再去通知栏点安装包，中间还容易被浏览器的「此类文件可能有害」拦一道。
 *
 * 这里每一步失败都能退回浏览器（见 MainActivity 的 onFallback），
 * 所以加了权限也不会把原来那条路堵死。
 */
object ApkInstaller {

    private val executor = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    /** 同一时刻只允许一个下载，连点「立即更新」不会下出两份。 */
    private val downloading = AtomicBoolean(false)

    /** 下载进度：[downloaded] / [total]，[total] 为 0 表示服务端没给长度。 */
    data class Progress(val downloaded: Long, val total: Long) {
        val percent: Int get() = if (total > 0) ((downloaded * 100) / total).toInt() else 0
        val known: Boolean get() = total > 0
    }

    /** Android 8 起「安装未知应用」是按应用授权的，装之前得先确认本应用被放行。 */
    fun canInstall(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O ||
            context.packageManager.canRequestPackageInstalls()

    /** 跳到系统里本应用的「安装未知应用」开关页。 */
    fun openInstallPermissionSettings(activity: Activity) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        runCatching {
            activity.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
                    .setData(Uri.parse("package:${activity.packageName}"))
            )
        }.onFailure {
            // 个别 ROM 没有这个页面，退到应用详情页也能找到同一个开关
            runCatching {
                activity.startActivity(
                    Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                        .setData(Uri.parse("package:${activity.packageName}"))
                )
            }
        }
    }

    /**
     * 下载升级包。[onProgress] 和 [onDone] 都在主线程回调。
     *
     * [expectedCode] 是后台登记的 versionCode，下完要核对——包是从网上取的，
     * 装之前必须确认它确实是本应用、且不比当前版本旧。
     */
    fun download(
        context: Context,
        url: String,
        expectedCode: Int,
        onProgress: (Progress) -> Unit,
        onDone: (File?, String?) -> Unit,
    ) {
        if (!downloading.compareAndSet(false, true)) return
        executor.execute {
            var result: File? = null
            var error: String? = null
            try {
                result = fetch(context, url) { progress -> main.post { onProgress(progress) } }
                error = verify(context, result, expectedCode)
                if (error != null) {
                    result.delete()
                    result = null
                }
            } catch (e: Exception) {
                error = e.message?.takeIf { it.isNotBlank() } ?: "下载失败"
            } finally {
                downloading.set(false)
            }
            main.post { onDone(result, error) }
        }
    }

    /** 调用方必须已经在后台线程。 */
    private fun fetch(context: Context, url: String, onProgress: (Progress) -> Unit): File {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        // 每次只留一个包：上次失败或装完没清的残留先扫掉，免得缓存越积越大
        dir.listFiles()?.forEach { it.delete() }
        val target = File(dir, "update.apk")

        var connection: HttpURLConnection? = null
        try {
            connection = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = 15_000
                readTimeout = 60_000
                instanceFollowRedirects = true
                setRequestProperty("User-Agent", "AutoLib-Android/${BuildConfig.VERSION_NAME}")
            }
            if (connection.responseCode !in 200..299) {
                throw IllegalStateException("服务器返回 ${connection.responseCode}")
            }
            val total = connection.contentLengthLong
            var downloaded = 0L
            var lastNotified = 0L
            connection.inputStream.use { input ->
                target.outputStream().use { output ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) break
                        output.write(buffer, 0, read)
                        downloaded += read
                        // 每 1% 或每 256KB 报一次就够了，回调太密只是白刷 UI
                        if (downloaded - lastNotified >= 256 * 1024) {
                            lastNotified = downloaded
                            onProgress(Progress(downloaded, total))
                        }
                    }
                }
            }
            onProgress(Progress(downloaded, total))
            return target
        } finally {
            connection?.disconnect()
        }
    }

    /**
     * 装之前核一遍包本身。返回 null 表示没问题，否则是给用户看的原因。
     *
     * 解析不出来多半是下到半截或者被网关塞了张错误页；包名对不上、版本比当前还旧
     * 则说明登记的地址指错了文件——这两种都不该往系统安装器送。
     */
    private fun verify(context: Context, file: File, expectedCode: Int): String? {
        if (!file.exists() || file.length() == 0L) return "下载的文件是空的"
        val info = context.packageManager.getPackageArchiveInfo(file.absolutePath, 0)
            ?: return "下载的文件不是有效的安装包"
        if (info.packageName != context.packageName) {
            return "安装包不是 AutoLib（${info.packageName}）"
        }
        val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode.toInt()
        } else {
            @Suppress("DEPRECATION") info.versionCode
        }
        if (code < expectedCode) return "安装包版本（$code）比登记的（$expectedCode）旧"
        if (code <= BuildConfig.VERSION_CODE) return "安装包不比当前版本新"
        return null
    }

    /** 把下好的包交给系统安装器。 */
    fun install(context: Context, file: File): Boolean {
        val uri = runCatching {
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        }.getOrNull() ?: return false
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            // 私有目录的文件，得把临时读权限一起授给安装器
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (intent.resolveActivity(context.packageManager) == null) return false
        return runCatching { context.startActivity(intent); true }.getOrDefault(false)
    }

    /** 安装成功后缓存里那份就没用了；装失败也无所谓，下次下载前照样会清。 */
    fun clearCache(context: Context) {
        runCatching { File(context.cacheDir, "updates").listFiles()?.forEach { it.delete() } }
    }
}
