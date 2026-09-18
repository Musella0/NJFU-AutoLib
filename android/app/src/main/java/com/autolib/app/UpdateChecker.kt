package com.autolib.app

import android.content.Context
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 客户端升级检查。
 *
 * 版本信息来自自己的服务端（`/api/app/version`），而不是 GitHub API：
 * 每次启动都要查，走自建服务更快也更可控，管理员改后台即可发版提示。
 *
 * 用 versionCode 而不是 versionName 比较——versionName 是给人看的字符串，
 * "0.2" 和 "0.10" 无法可靠地比大小。
 */
object UpdateChecker {
    private const val PREFS = "update_checker"
    private const val KEY_LAST_CHECK = "last_check_date"
    private const val KEY_SKIPPED = "skipped_version_code"
    private const val KEY_AUTO = "auto_check_enabled"

    data class Release(
        val versionCode: Int,
        val versionName: String,
        val downloadUrl: String,
        val notes: String,
    ) {
        val isNewer: Boolean get() = versionCode > BuildConfig.VERSION_CODE
    }

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun today() = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    /** 「永不提示」只关掉自动检查，设置页里手动检查始终可用。 */
    fun autoCheckEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_AUTO, true)

    fun setAutoCheckEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_AUTO, enabled).apply()
    }

    fun skip(context: Context, versionCode: Int) {
        prefs(context).edit().putInt(KEY_SKIPPED, versionCode).apply()
    }

    /** 跳过只对该版本生效，之后更新的版本仍会提示。 */
    private fun isSkipped(context: Context, versionCode: Int) =
        prefs(context).getInt(KEY_SKIPPED, 0) >= versionCode

    /** 自动检查每天至多一次，避免每次切回前台都打一次网络。 */
    private fun shouldAutoCheck(context: Context): Boolean {
        if (!autoCheckEnabled(context)) return false
        return prefs(context).getString(KEY_LAST_CHECK, "") != today()
    }

    private fun markChecked(context: Context) {
        prefs(context).edit().putString(KEY_LAST_CHECK, today()).apply()
    }

    private fun parse(json: JSONObject?): Release? {
        json ?: return null
        val code = json.optInt("version_code", 0)
        val url = json.optString("download_url")
        if (code <= 0 || url.isBlank()) return null
        return Release(
            versionCode = code,
            versionName = json.optString("version_name"),
            downloadUrl = url,
            notes = json.optString("notes"),
        )
    }

    /**
     * 启动时的静默检查：仅在有更新、且未被跳过时回调，其余情况一律不打扰。
     */
    fun checkSilently(context: Context, api: NativeApi, onUpdate: (Release) -> Unit) {
        if (!shouldAutoCheck(context)) return
        api.get("/api/app/version") { response ->
            if (!response.ok) return@get          // 网络异常不提示，等明天
            markChecked(context)
            val release = parse(response.jsonObject) ?: return@get
            if (release.isNewer && !isSkipped(context, release.versionCode)) onUpdate(release)
        }
    }

    /**
     * 设置页手动检查：无论有没有更新都要给回应，所以把「已是最新」也回调出去。
     * 手动检查会无视之前的「跳过」，用户主动点了就是想看结果。
     */
    fun checkManually(context: Context, api: NativeApi, onResult: (Release?, String?) -> Unit) {
        api.get("/api/app/version") { response ->
            if (!response.ok) {
                onResult(null, response.message("检查更新失败"))
                return@get
            }
            markChecked(context)
            val release = parse(response.jsonObject)
            when {
                release == null -> onResult(null, "服务端还没有发布版本信息")
                release.isNewer -> onResult(release, null)
                else -> onResult(null, null)      // 已是最新
            }
        }
    }
}
