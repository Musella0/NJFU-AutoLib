package com.autolib.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray

/**
 * 每天抢座结束后在后台拉一次结果，写进 [ReservationCache] 并发本地通知。
 *
 * 只调 `/api/my/reservation_results`——它是纯数据库读取，不会触发学校系统登录，
 * 因此能在 BroadcastReceiver 的时限内跑完。座位号、时段这类结构化信息由前台
 * 的 MainActivity 负责刷新。
 */
object ReservationSync {
    const val CHANNEL_ID = "reservation_result"
    private const val NOTIFICATION_ID = 2001

    /** 阻塞执行，调用方必须已经在后台线程。 */
    fun sync(context: Context, notify: Boolean) {
        val app = context.applicationContext
        val response = NativeApi(app).getBlocking("/api/my/reservation_results")
        val results = response.jsonArray ?: return
        if (results.length() == 0) return

        val latest = pickLatest(results) ?: return
        val pid = latest.optString("pid")
        val text = latest.optString("result").trim()
        if (text.isBlank()) return

        ReservationCache.saveSummary(app, pid, text.lineSequence().first().trim())
        if (!notify) return

        val success = latest.optBoolean("success")
        // updated_at 一起进指纹，这样第二天同样的文案还能再提醒一次
        val signature = "${latest.optString("updated_at")}|$text"
        if (!ReservationCache.shouldNotify(app, signature)) return
        if (notifyResult(app, pid, success, text)) {
            ReservationCache.markNotified(app, signature)
        }
    }

    private fun pickLatest(results: JSONArray): org.json.JSONObject? =
        (0 until results.length())
            .mapNotNull { results.optJSONObject(it) }
            .maxByOrNull { it.optString("updated_at") }

    /** 返回是否真的发出去了——通知权限没给时不该记成"已通知"。 */
    private fun notifyResult(context: Context, pid: String, success: Boolean, text: String): Boolean {
        if (!hasPermission(context)) return false
        ensureChannel(context)

        val open = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val title = if (success) "✅ 抢座成功" else "❌ 抢座失败"
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_home_native)
            .setContentTitle(if (pid.isBlank()) title else "$title · $pid")
            .setContentText(text.lineSequence().first().trim())
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        return try {
            NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    fun hasPermission(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(
                context,
                android.Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED

    fun ensureChannel(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "抢座结果", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "每天自动抢座完成后推送结果"
            }
        )
    }
}
