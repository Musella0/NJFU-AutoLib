package com.autolib.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject

/** Pulls confirmed school-closure announcements without requiring FCM. */
object SchoolAnnouncementSync {
    const val CHANNEL_ID = "school_closure"
    private const val PREFS = "school_announcements"
    private const val NOTIFICATION_BASE = 2100

    fun sync(context: Context) {
        val response = NativeApi(context.applicationContext).getBlocking("/api/announcements")
        val announcements = response.jsonArray ?: return
        notifyNew(context.applicationContext, announcements)
    }

    fun notifyNew(context: Context, announcements: JSONArray) {
        for (i in 0 until announcements.length()) {
            val item = announcements.optJSONObject(i) ?: continue
            if (!item.optBoolean("popup_required")) continue
            val signature = signature(item)
            if (prefs(context).getBoolean("notified_$signature", false)) continue
            if (notify(context, item, signature)) {
                prefs(context).edit().putBoolean("notified_$signature", true).apply()
            }
        }
    }

    fun firstUndismissed(context: Context, announcements: JSONArray): JSONObject? {
        for (i in 0 until announcements.length()) {
            val item = announcements.optJSONObject(i) ?: continue
            if (!item.optBoolean("popup_required")) continue
            if (!prefs(context).getBoolean("dismissed_${signature(item)}", false)) return item
        }
        return null
    }

    fun dismissPopup(context: Context, item: JSONObject) {
        prefs(context).edit().putBoolean("dismissed_${signature(item)}", true).apply()
    }

    private fun signature(item: JSONObject): String {
        val id = item.optString("source_review_id").ifBlank { item.optString("id", "unknown") }
        return "${id}_r${item.optInt("revision", 1)}"
            .replace(Regex("[^A-Za-z0-9_.-]"), "_")
    }

    private fun notify(context: Context, item: JSONObject, signature: String): Boolean {
        if (!ReservationSync.hasPermission(context)) return false
        ensureChannel(context)
        val open = PendingIntent.getActivity(
            context,
            signature.hashCode(),
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val title = item.optString("title").ifBlank { "图书馆闭馆公告" }
        val content = item.optString("content").trim().take(4_000)
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_home_native)
            .setContentTitle(title)
            .setContentText(content.lineSequence().firstOrNull().orEmpty())
            .setStyle(NotificationCompat.BigTextStyle().bigText(content))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        return try {
            val id = NOTIFICATION_BASE + (signature.hashCode() and Int.MAX_VALUE) % 700
            NotificationManagerCompat.from(context).notify(id, notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "学校闭馆公告", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "已人工确认的闭馆公告和预约暂停时段"
            }
        )
    }

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}
