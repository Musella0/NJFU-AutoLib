package com.autolib.app

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import java.util.Calendar
import java.util.TimeZone

/** Daily local poll after the server's 20:00 Beijing notice scan and retries. */
object SchoolAnnouncementAlarm {
    private const val CHECK_HOUR = 20
    private const val CHECK_MINUTE = 30
    private const val REQUEST_CODE = 1002
    const val ACTION_CHECK = "com.autolib.app.CHECK_SCHOOL_ANNOUNCEMENTS"

    fun schedule(context: Context) {
        val app = context.applicationContext
        val manager = app.getSystemService(AlarmManager::class.java) ?: return
        val zone = TimeZone.getTimeZone("Asia/Shanghai")
        val now = Calendar.getInstance(zone)
        val target = Calendar.getInstance(zone).apply {
            set(Calendar.HOUR_OF_DAY, CHECK_HOUR)
            set(Calendar.MINUTE, CHECK_MINUTE)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
            if (!after(now)) add(Calendar.DAY_OF_YEAR, 1)
        }
        manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, target.timeInMillis, intent(app))
    }

    private fun intent(context: Context): PendingIntent = PendingIntent.getBroadcast(
        context,
        REQUEST_CODE,
        Intent(context, SchoolAnnouncementReceiver::class.java).setAction(ACTION_CHECK),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )
}

class SchoolAnnouncementReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != SchoolAnnouncementAlarm.ACTION_CHECK) return
        val app = context.applicationContext
        val pending = goAsync()
        Thread {
            try {
                SchoolAnnouncementSync.sync(app)
            } catch (_: Exception) {
                // A foreground refresh or the next daily poll will retry.
            } finally {
                SchoolAnnouncementAlarm.schedule(app)
                pending.finish()
            }
        }.start()
    }
}
