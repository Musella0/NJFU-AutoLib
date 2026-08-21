package com.autolib.app

import android.content.Context
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * 今日预约的本地快照，是通知和桌面小组件共同的数据源。
 *
 * 小组件由桌面进程渲染、每次回调都是新实例，不能持有状态也不该发网络请求，
 * 所以一律读这份缓存。写入方有两个：前台的 MainActivity 每次查到预约后写，
 * 以及每天闹钟触发的后台同步。
 */
object ReservationCache {
    private const val PREFS = "reservation_cache"
    private const val KEY_DATE = "date"
    private const val KEY_PID = "pid"
    private const val KEY_SEAT = "seat"
    private const val KEY_BEGIN = "begin"
    private const val KEY_END = "end"
    private const val KEY_STATUS = "status"
    private const val KEY_TOMORROW_SEAT = "tomorrow_seat"
    private const val KEY_TOMORROW_BEGIN = "tomorrow_begin"
    private const val KEY_TOMORROW_END = "tomorrow_end"
    private const val KEY_SUMMARY = "summary"
    private const val KEY_UPDATED_AT = "updated_at"
    private const val KEY_NOTIFIED = "notified_signature"
    private const val KEY_STATUS_CODE = "status_code"
    private const val KEY_ARRIVED_DATE = "arrived_date"
    private const val KEY_LATE_PROTECTION = "late_protection"
    private const val KEY_RUNNING = "running"
    private const val KEY_MODE = "mode"
    private const val KEY_WEEK = "week_segments"

    data class Snapshot(
        val date: String,
        val pid: String,
        val seat: String,
        val begin: String,
        val end: String,
        val status: String,
        /** 学校系统的 resvStatus 原始码，缓存里没有时为 -1。 */
        val statusCode: Int,
        val tomorrowSeat: String,
        val tomorrowBegin: String,
        val tomorrowEnd: String,
        /** 没有结构化座位信息时的兜底文案，例如后台抓到的抢座结果首行。 */
        val summary: String,
        val updatedAt: Long,
        /** 已标记到馆的日期；到馆与否按「是否等于今天」判断，跨天自动失效。 */
        val arrivedDate: String,
        val lateProtection: Boolean,
        /** 自动预约是否开启（is_reserved）。 */
        val running: Boolean,
        val mode: String,
        /** {"1":["08:00-22:00"],...} 周一到周日实际生效的时段。 */
        val weekJson: String,
    ) {
        val hasSeat: Boolean get() = seat.isNotBlank()
        val hasTomorrowSeat: Boolean get() = tomorrowSeat.isNotBlank()
        val isToday: Boolean get() = date == todayKey()
        val arrived: Boolean get() = arrivedDate == todayKey()

        fun weekSegments(iso: Int): List<String> {
            val array = runCatching { JSONObject(weekJson).optJSONArray(iso.toString()) }.getOrNull()
                ?: return emptyList()
            return (0 until array.length()).map { array.optString(it) }.filter { it.contains('-') }
        }
    }

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun todayKey(): String = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    fun read(context: Context): Snapshot = prefs(context).let {
        Snapshot(
            date = it.getString(KEY_DATE, "").orEmpty(),
            pid = it.getString(KEY_PID, "").orEmpty(),
            seat = it.getString(KEY_SEAT, "").orEmpty(),
            begin = it.getString(KEY_BEGIN, "").orEmpty(),
            end = it.getString(KEY_END, "").orEmpty(),
            status = it.getString(KEY_STATUS, "").orEmpty(),
            statusCode = it.getInt(KEY_STATUS_CODE, -1),
            tomorrowSeat = it.getString(KEY_TOMORROW_SEAT, "").orEmpty(),
            tomorrowBegin = it.getString(KEY_TOMORROW_BEGIN, "").orEmpty(),
            tomorrowEnd = it.getString(KEY_TOMORROW_END, "").orEmpty(),
            summary = it.getString(KEY_SUMMARY, "").orEmpty(),
            updatedAt = it.getLong(KEY_UPDATED_AT, 0L),
            arrivedDate = it.getString(KEY_ARRIVED_DATE, "").orEmpty(),
            lateProtection = it.getBoolean(KEY_LATE_PROTECTION, false),
            running = it.getBoolean(KEY_RUNNING, false),
            mode = it.getString(KEY_MODE, "").orEmpty(),
            weekJson = it.getString(KEY_WEEK, "").orEmpty(),
        )
    }

    private fun seatOf(reservation: JSONObject?) =
        reservation?.optJSONObject("devInfo")?.optString("devName").orEmpty()

    private fun timeOf(reservation: JSONObject?, field: String) =
        reservation?.optString(field).orEmpty().substringAfter(' ', "").take(5)

    /** 前台查到预约后调用；参数为 null 表示当天确实没有预约。 */
    fun saveReservations(
        context: Context,
        pid: String,
        today: JSONObject?,
        todayStatus: String,
        tomorrow: JSONObject?,
    ) {
        prefs(context).edit()
            .putString(KEY_DATE, todayKey())
            .putString(KEY_PID, pid)
            .putString(KEY_SEAT, seatOf(today))
            .putString(KEY_BEGIN, timeOf(today, "resvBeginTime"))
            .putString(KEY_END, timeOf(today, "resvEndTime"))
            .putString(KEY_STATUS, if (today == null) "今日暂无预约" else todayStatus)
            .putInt(KEY_STATUS_CODE, today?.optInt("resvStatus", -1) ?: -1)
            .putString(KEY_TOMORROW_SEAT, seatOf(tomorrow))
            .putString(KEY_TOMORROW_BEGIN, timeOf(tomorrow, "resvBeginTime"))
            .putString(KEY_TOMORROW_END, timeOf(tomorrow, "resvEndTime"))
            .putLong(KEY_UPDATED_AT, System.currentTimeMillis())
            .apply()
        SeatWidgets.refresh(context)
    }

    /** 账号配置加载后调用，缓存小组件要用的周计划与保护状态。 */
    fun savePlan(
        context: Context,
        running: Boolean,
        mode: String,
        weekJson: String,
        arrivedDate: String,
        lateProtection: Boolean,
    ) {
        prefs(context).edit()
            .putBoolean(KEY_RUNNING, running)
            .putString(KEY_MODE, mode)
            .putString(KEY_WEEK, weekJson)
            .putString(KEY_ARRIVED_DATE, arrivedDate)
            .putBoolean(KEY_LATE_PROTECTION, lateProtection)
            .apply()
        SeatWidgets.refresh(context)
    }

    /** 小组件里点「我已到馆」成功后回写，让三个组件立即变化。 */
    fun saveArrived(context: Context, arrived: Boolean) {
        prefs(context).edit()
            .putString(KEY_ARRIVED_DATE, if (arrived) todayKey() else "")
            .apply()
        SeatWidgets.refresh(context)
    }

    /** 后台同步只拿得到抢座结果文本时调用，不覆盖已有的结构化座位信息。 */
    fun saveSummary(context: Context, pid: String, summary: String) {
        val store = prefs(context)
        val editor = store.edit()
            .putString(KEY_PID, pid)
            .putString(KEY_SUMMARY, summary)
            .putLong(KEY_UPDATED_AT, System.currentTimeMillis())
        if (store.getString(KEY_DATE, "") != todayKey()) {
            // 跨天了，昨天的座位信息不能再显示
            editor.putString(KEY_DATE, todayKey())
                .putString(KEY_SEAT, "")
                .putString(KEY_BEGIN, "")
                .putString(KEY_END, "")
                .putString(KEY_STATUS, "")
                .putInt(KEY_STATUS_CODE, -1)
                .putString(KEY_TOMORROW_SEAT, "")
                .putString(KEY_TOMORROW_BEGIN, "")
                .putString(KEY_TOMORROW_END, "")
        }
        editor.apply()
        SeatWidgets.refresh(context)
    }

    fun clear(context: Context) {
        prefs(context).edit().clear().apply()
        SeatWidgets.refresh(context)
    }

    /** 通知去重：同样的内容当天只提醒一次。 */
    fun shouldNotify(context: Context, signature: String): Boolean =
        prefs(context).getString(KEY_NOTIFIED, "") != signature

    fun markNotified(context: Context, signature: String) {
        prefs(context).edit().putString(KEY_NOTIFIED, signature).apply()
    }

    fun formatUpdatedAt(updatedAt: Long): String {
        if (updatedAt <= 0L) return "尚未同步"
        val calendar = Calendar.getInstance().apply { timeInMillis = updatedAt }
        return SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(calendar.time)
    }
}
