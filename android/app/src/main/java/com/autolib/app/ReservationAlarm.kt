package com.autolib.app

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import java.util.Calendar

/**
 * 每天在抢座结束后唤醒一次去查结果。
 *
 * 用 setAndAllowWhileIdle 而不是 setExact：前者在 Doze 下照样能触发，而且不需要
 * Android 12+ 那个要用户手动去系统设置里开的 SCHEDULE_EXACT_ALARM 权限。代价是
 * 触发时刻可能有几分钟浮动，对"看今天抢到没有"这个场景完全够用。
 */
object ReservationAlarm {
    /** 后端固定 7:00 抢座，留几分钟余量再查结果。 */
    private const val CHECK_HOUR = 7
    private const val CHECK_MINUTE = 5
    private const val REQUEST_CODE = 1001
    const val ACTION_CHECK = "com.autolib.app.CHECK_RESERVATION"

    private const val PREFS = "reservation_alarm"
    private const val KEY_ENABLED = "enabled"

    /** 抢座结果是这个 App 的核心信息，默认就该推送；用户可以在设置页关掉。 */
    fun isEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_ENABLED, true)

    fun setEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply()
        if (enabled) schedule(context) else cancel(context)
    }

    fun schedule(context: Context) {
        if (!isEnabled(context)) return
        val manager = context.getSystemService(AlarmManager::class.java) ?: return
        val now = Calendar.getInstance()
        val target = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, CHECK_HOUR)
            set(Calendar.MINUTE, CHECK_MINUTE)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
            if (!after(now)) add(Calendar.DAY_OF_YEAR, 1)
        }
        manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, target.timeInMillis, intent(context))
    }

    fun cancel(context: Context) {
        context.getSystemService(AlarmManager::class.java)?.cancel(intent(context))
    }

    fun describe(): String = "每天 %02d:%02d 检查".format(CHECK_HOUR, CHECK_MINUTE)

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun intent(context: Context): PendingIntent = PendingIntent.getBroadcast(
        context.applicationContext,
        REQUEST_CODE,
        Intent(context.applicationContext, ReservationReceiver::class.java).setAction(ACTION_CHECK),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )
}

/**
 * 开机后重新排程——AlarmManager 的闹钟不会跨重启保留。
 *
 * 单独拆一个 receiver 是因为它必须 exported：BOOT_COMPLETED 由系统进程投递。
 * 这个 action 是 protected broadcast，只有系统能发，所以导出没有风险。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            ReservationAlarm.schedule(context.applicationContext)
            SchoolAnnouncementAlarm.schedule(context.applicationContext)
        }
    }
}

/** 闹钟触发点。只接自己发的显式 Intent，不需要导出。 */
class ReservationReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext
        if (intent.action != ReservationAlarm.ACTION_CHECK) return
        val pending = goAsync()
        Thread {
            try {
                ReservationSync.sync(app, notify = true)
            } catch (_: Exception) {
                // 后台同步失败就等下一天，不打扰用户
            } finally {
                ReservationAlarm.schedule(app)
                pending.finish()
            }
        }.start()
    }
}
