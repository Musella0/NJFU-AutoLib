package com.autolib.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.View
import android.widget.RemoteViews
import android.widget.Toast
import androidx.core.content.ContextCompat
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * 桌面小组件，三个规格共用 [ReservationCache] 一个数据源：
 *
 * - SMALL  2×2 专注今天：座位、进度、「我已到馆」
 * - MEDIUM 4×2 今天 + 明日两栏
 * - LARGE  4×4 完整一天 + 明日 + 本周节奏
 *
 * 小组件由桌面进程渲染，只能用 RemoteViews 白名单里的控件，「本周节奏」这类
 * 图表画进 Bitmap 塞给 ImageView。「我已到馆」是唯一在组件里直接发请求的动作
 * （一次纯数据库写，不触发学校登录）；午休 / 取消 / 调整明日需要确认或选时间，
 * 深链进 App 打开对应对话框。
 */
object SeatWidgets {
    const val ACTION_ARRIVED = "com.autolib.app.WIDGET_ARRIVED"

    /** 深链动作前缀，MainActivity 按后缀打开对应对话框。 */
    const val ACTION_PREFIX = "com.autolib.app.widget."

    private val ACTIVE_CODES = setOf(1027, 1093, 3141)
    private val BREACHED_CODES = setOf(1169, 3281)
    private const val CODE_RESERVED = 1027
    private const val CODE_FINISHED = 3265

    /** 「本周节奏」以上的固定内容高度，以及行高的上下限（dp）。 */
    private const val CHART_TOP_DP = 300
    private const val WEEK_ROW_MIN_DP = 15f
    private const val WEEK_ROW_MAX_DP = 46f
    /** 行高再大色条也不跟着变粗，多出来的空间留作行距。 */
    private const val WEEK_BAR_MAX_DP = 14f
    private val DAY_SHORT = listOf("一", "二", "三", "四", "五", "六", "日")

    private val PROVIDERS = listOf(
        SeatWidgetSmallProvider::class.java,
        SeatWidgetProvider::class.java,
        SeatWidgetLargeProvider::class.java,
    )

    /** 数据变了就叫桌面把三个规格都重画一次。没有添加小组件时是无害的空操作。 */
    fun refresh(context: Context) {
        val app = context.applicationContext
        val manager = AppWidgetManager.getInstance(app) ?: return
        PROVIDERS.forEach { cls ->
            val ids = manager.getAppWidgetIds(ComponentName(app, cls))
            if (ids.isEmpty()) return@forEach
            app.sendBroadcast(
                Intent(app, cls)
                    .setAction(AppWidgetManager.ACTION_APPWIDGET_UPDATE)
                    .putExtra(AppWidgetManager.EXTRA_APPWIDGET_IDS, ids)
            )
        }
    }

    // region 三个规格

    fun buildSmall(context: Context): RemoteViews {
        val s = ReservationCache.read(context)
        val views = RemoteViews(context.packageName, R.layout.widget_small)
        views.setTextViewText(R.id.w_date, "今日 · " + SimpleDateFormat("E M/d", Locale.CHINA).format(Date()))
        views.setOnClickPendingIntent(R.id.w_root, openApp(context, "open", 20))

        val fresh = s.isToday
        when {
            !fresh -> {
                badge(context, views, R.id.w_badge, null)
                views.setTextViewText(R.id.w_seat, "—")
                views.setTextViewText(R.id.w_time, "打开 App 同步")
                views.setViewVisibility(R.id.w_progress, View.GONE)
                views.setTextViewText(R.id.w_status, "")
                action(context, views, R.id.w_action, "打开 App", accent = true, intent = openApp(context, "open", 20))
            }
            !s.hasSeat -> {
                badge(context, views, R.id.w_badge, null)
                views.setTextViewText(R.id.w_seat, "暂无预约")
                views.setTextViewText(R.id.w_time, if (s.summary.isNotBlank()) s.summary else "今天还没有座位")
                views.setViewVisibility(R.id.w_progress, View.GONE)
                views.setTextViewText(R.id.w_status, "")
                action(context, views, R.id.w_action, "⚡ 立即预约", accent = true, intent = openApp(context, "reserve", 24))
            }
            else -> {
                badge(context, views, R.id.w_badge, statusBadge(s))
                views.setTextViewText(R.id.w_seat, s.seat)
                // 2×2 只有一行的宽度，加上时长就会被截断；时长信息由下面的「剩余」行承担
                views.setTextViewText(R.id.w_time, "${s.begin} – ${s.end}")
                views.setViewVisibility(R.id.w_progress, View.VISIBLE)
                views.setProgressBar(R.id.w_progress, 100, progressPercent(s), false)
                views.setTextViewText(R.id.w_status, statusLine(context, s, compact = true))
                bindPrimaryAction(context, views, R.id.w_action, s)
            }
        }
        return views
    }

    fun buildMedium(context: Context): RemoteViews {
        val s = ReservationCache.read(context)
        val views = RemoteViews(context.packageName, R.layout.widget_medium)
        views.setOnClickPendingIntent(R.id.w_root, openApp(context, "open", 20))

        // 今日栏
        val fresh = s.isToday
        when {
            !fresh -> {
                views.setTextViewText(R.id.w_seat, "—")
                views.setTextViewText(R.id.w_time, "打开 App 同步")
                badge(context, views, R.id.w_badge, null)
                views.setViewVisibility(R.id.w_progress, View.GONE)
                views.setTextViewText(R.id.w_status, "")
                action(context, views, R.id.w_action, "打开 App", accent = true, intent = openApp(context, "open", 20))
            }
            !s.hasSeat -> {
                views.setTextViewText(R.id.w_seat, "暂无预约")
                views.setTextViewText(R.id.w_time, if (s.summary.isNotBlank()) s.summary else "今天还没有座位")
                badge(context, views, R.id.w_badge, null)
                views.setViewVisibility(R.id.w_progress, View.GONE)
                views.setTextViewText(R.id.w_status, "")
                action(context, views, R.id.w_action, "⚡ 立即预约", accent = true, intent = openApp(context, "reserve", 24))
            }
            else -> {
                views.setTextViewText(R.id.w_seat, s.seat)
                views.setTextViewText(R.id.w_time, "${s.begin}–${s.end} · ${durationText(s.begin, s.end)}")
                badge(context, views, R.id.w_badge, statusBadge(s))
                views.setViewVisibility(R.id.w_progress, View.VISIBLE)
                views.setProgressBar(R.id.w_progress, 100, progressPercent(s), false)
                views.setTextViewText(R.id.w_status, statusLine(context, s))
                bindPrimaryAction(context, views, R.id.w_action, s)
            }
        }

        // 明日栏
        fillTomorrow(context, views, s,
            seatId = R.id.w_tmr_seat, timeId = R.id.w_tmr_time,
            badgeId = R.id.w_tmr_badge, hintId = R.id.w_tmr_hint)
        views.setOnClickPendingIntent(R.id.w_tmr_action, openApp(context, "tomorrow", 23))
        return views
    }

    /**
     * [minHeightDp] 是宿主分配给这个组件的高度。不同桌面的网格差异很大，
     * 固定行高会在高单元格里留下一大片空白，所以把「本周节奏」的行高摊到剩余空间上。
     */
    fun buildLarge(context: Context, minHeightDp: Int = 0): RemoteViews {
        val s = ReservationCache.read(context)
        val views = RemoteViews(context.packageName, R.layout.widget_large)
        views.setTextViewText(R.id.w_date, SimpleDateFormat("E · M月d日", Locale.CHINA).format(Date()))
        views.setOnClickPendingIntent(R.id.w_root, openApp(context, "open", 20))

        val fresh = s.isToday
        val hasToday = fresh && s.hasSeat
        if (hasToday) {
            badge(context, views, R.id.w_badge, statusBadge(s))
            badge(context, views, R.id.w_badge2, secondaryBadge(s))
            views.setTextViewText(R.id.w_seat, s.seat)
            views.setTextViewText(R.id.w_time, "${s.begin} – ${s.end} · 共 ${durationText(s.begin, s.end)}")
            views.setViewVisibility(R.id.w_progress, View.VISIBLE)
            views.setProgressBar(R.id.w_progress, 100, progressPercent(s), false)
            val now = nowMinutes()
            val begin = minutesOf(s.begin)
            val end = minutesOf(s.end)
            when {
                now < begin -> {
                    views.setTextViewText(R.id.w_elapsed, "未开始")
                    views.setTextViewText(R.id.w_remaining, "${s.begin} 开始")
                }
                now < end -> {
                    views.setTextViewText(R.id.w_elapsed, "已过 ${formatMinutes(now - begin)}")
                    views.setTextViewText(R.id.w_remaining, "剩余 ${formatMinutes(end - now)}")
                }
                else -> {
                    views.setTextViewText(R.id.w_elapsed, "已结束")
                    views.setTextViewText(R.id.w_remaining, "")
                }
            }
            bindPrimaryAction(context, views, R.id.w_action, s)
            views.setViewVisibility(R.id.w_action_nap, View.VISIBLE)
            views.setViewVisibility(R.id.w_action_cancel, View.VISIBLE)
            views.setOnClickPendingIntent(R.id.w_action_nap, openApp(context, "nap", 21))
            views.setOnClickPendingIntent(R.id.w_action_cancel, openApp(context, "cancel", 22))
        } else {
            badge(context, views, R.id.w_badge, null)
            badge(context, views, R.id.w_badge2, null)
            views.setTextViewText(R.id.w_seat, if (fresh) "暂无预约" else "—")
            views.setTextViewText(R.id.w_time, when {
                !fresh -> "打开 App 同步"
                s.summary.isNotBlank() -> s.summary
                else -> "今天还没有座位"
            })
            views.setViewVisibility(R.id.w_progress, View.GONE)
            views.setTextViewText(R.id.w_elapsed, "")
            views.setTextViewText(R.id.w_remaining, "")
            views.setViewVisibility(R.id.w_action_nap, View.GONE)
            views.setViewVisibility(R.id.w_action_cancel, View.GONE)
            if (fresh) action(context, views, R.id.w_action, "⚡ 立即预约", accent = true, intent = openApp(context, "reserve", 24))
            else action(context, views, R.id.w_action, "打开 App", accent = true, intent = openApp(context, "open", 20))
        }

        fillTomorrow(context, views, s,
            seatId = R.id.w_tmr_seat, timeId = R.id.w_tmr_time,
            badgeId = null, hintId = null)
        views.setViewVisibility(R.id.w_tmr_pill, if (s.running) View.VISIBLE else View.GONE)
        views.setOnClickPendingIntent(R.id.w_tmr_pill, openApp(context, "tomorrow", 23))

        views.setTextViewText(R.id.w_week_sub, weekSubtitle(s))
        // 图表以上的固定内容实测约 300dp；剩下的高度七等分，超出上限就不再拉伸
        val rowDp = ((minHeightDp - CHART_TOP_DP) / 7f).coerceIn(WEEK_ROW_MIN_DP, WEEK_ROW_MAX_DP)
        views.setImageViewBitmap(R.id.w_week_chart, weekBitmap(context, s, rowDp))
        return views
    }

    // endregion

    // region 明日栏与动作

    private fun fillTomorrow(
        context: Context,
        views: RemoteViews,
        s: ReservationCache.Snapshot,
        seatId: Int,
        timeId: Int,
        badgeId: Int?,
        hintId: Int?,
        withDuration: Boolean = true,
    ) {
        val fresh = s.isToday
        val tomorrowIso = isoToday() % 7 + 1
        val planned = if (s.running) s.weekSegments(tomorrowIso) else emptyList()
        when {
            fresh && s.hasTomorrowSeat -> {
                views.setTextViewText(seatId, s.tomorrowSeat)
                views.setTextViewText(timeId, "${s.tomorrowBegin}–${s.tomorrowEnd}" +
                    if (withDuration) " · ${durationText(s.tomorrowBegin, s.tomorrowEnd)}" else "")
                badgeId?.let { badge(context, views, it, BadgeStyle("已预约", R.drawable.bg_widget_badge_tomorrow, R.color.tomorrow)) }
                hintId?.let {
                    views.setTextViewText(it, "✓ 明日座位已预约成功")
                    views.setViewVisibility(it, View.VISIBLE)
                }
            }
            planned.isNotEmpty() -> {
                views.setTextViewText(seatId, "自动抢座")
                views.setTextViewText(timeId, if (planned.size > 1)
                    "${planned.size} 段 · ${planned.first().replace("-", " – ")}"
                else planned.first().replace("-", " – "))
                badgeId?.let { badge(context, views, it, null) }
                hintId?.let {
                    views.setTextViewText(it, "🌅 明早 07:00 按配置自动抢座")
                    views.setViewVisibility(it, View.VISIBLE)
                }
            }
            else -> {
                views.setTextViewText(seatId, "—")
                views.setTextViewText(timeId, if (s.running) "明日休息" else "自动预约已暂停")
                badgeId?.let { badge(context, views, it, null) }
                hintId?.let {
                    views.setTextViewText(it, if (s.running) "" else "去 App 开启自动预约")
                    views.setViewVisibility(it, View.VISIBLE)
                }
            }
        }
    }

    /** 今日主按钮：活跃预约时是「我已到馆」，已到馆后变成打开 App 的确认样式。 */
    private fun bindPrimaryAction(context: Context, views: RemoteViews, id: Int, s: ReservationCache.Snapshot) {
        val code = s.statusCode
        when {
            code == CODE_FINISHED ->
                action(context, views, id, "我还能学！", accent = true, intent = openApp(context, "reserve", 24))
            code in BREACHED_CODES ->
                action(context, views, id, "⚡ 再次预约", accent = true, intent = openApp(context, "reserve", 24))
            s.arrived ->
                action(context, views, id, "✓ 已到馆", accent = false, intent = openApp(context, "open", 20))
            else ->
                action(context, views, id, "✓ 我已到馆", accent = true,
                    intent = PendingIntent.getBroadcast(
                        context, 30,
                        Intent(context, SeatWidgetProvider::class.java).setAction(ACTION_ARRIVED),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                    ))
        }
    }

    private fun action(
        context: Context,
        views: RemoteViews,
        id: Int,
        label: String,
        accent: Boolean,
        intent: PendingIntent,
    ) {
        views.setTextViewText(id, label)
        views.setInt(id, "setBackgroundResource",
            if (accent) R.drawable.bg_widget_btn_accent else R.drawable.bg_widget_btn_outline)
        views.setTextColor(id, ContextCompat.getColor(context,
            if (accent) R.color.on_primary else R.color.text_primary))
        views.setOnClickPendingIntent(id, intent)
    }

    private fun openApp(context: Context, action: String, requestCode: Int): PendingIntent =
        PendingIntent.getActivity(
            context,
            requestCode,
            Intent(context, MainActivity::class.java)
                .setAction(ACTION_PREFIX + action)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
                        or Intent.FLAG_ACTIVITY_SINGLE_TOP
                ),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    // endregion

    // region 状态徽章与进度

    private data class BadgeStyle(val text: String, val bg: Int, val textColor: Int)

    private fun badge(context: Context, views: RemoteViews, id: Int, style: BadgeStyle?) {
        if (style == null) {
            views.setViewVisibility(id, View.GONE)
            return
        }
        views.setViewVisibility(id, View.VISIBLE)
        views.setTextViewText(id, style.text)
        views.setInt(id, "setBackgroundResource", style.bg)
        views.setTextColor(id, ContextCompat.getColor(context, style.textColor))
    }

    private fun statusBadge(s: ReservationCache.Snapshot): BadgeStyle {
        val text = s.status.ifBlank { "已预约" }
        return when (s.statusCode) {
            1093 -> BadgeStyle(text, R.drawable.bg_widget_badge_success, R.color.success)
            3141 -> BadgeStyle(text, R.drawable.bg_widget_badge_accent, R.color.primary)
            CODE_FINISHED -> BadgeStyle(text, R.drawable.bg_widget_badge_muted, R.color.text_muted)
            in BREACHED_CODES -> BadgeStyle(text, R.drawable.bg_widget_badge_danger, R.color.danger)
            else -> BadgeStyle(text, R.drawable.bg_widget_badge_success, R.color.success)
        }
    }

    /** 大号组件的第二枚徽章：已到馆 > 待签到 > 迟到保护。 */
    private fun secondaryBadge(s: ReservationCache.Snapshot): BadgeStyle? = when {
        s.statusCode !in ACTIVE_CODES -> null
        s.arrived || s.statusCode == 1093 ->
            BadgeStyle("✓ 已到馆", R.drawable.bg_widget_badge_success, R.color.success)
        s.statusCode == CODE_RESERVED && nowMinutes() >= minutesOf(s.begin) ->
            BadgeStyle("待签到", R.drawable.bg_widget_badge_danger, R.color.danger)
        s.lateProtection -> BadgeStyle("🛡 迟到保护", R.drawable.bg_widget_badge_accent, R.color.primary)
        else -> null
    }

    private fun progressPercent(s: ReservationCache.Snapshot): Int {
        val begin = minutesOf(s.begin)
        val end = minutesOf(s.end)
        if (end <= begin) return 0
        return (((nowMinutes() - begin) * 100) / (end - begin)).coerceIn(0, 100)
    }

    /**
     * 「进行中 · 剩余 3小时40分」，剩余部分标橙色。
     * [compact] 供 2×2 使用：那一行放不下前缀，而状态徽章已经说明了进行中还是暂离。
     */
    private fun statusLine(
        context: Context,
        s: ReservationCache.Snapshot,
        compact: Boolean = false,
    ): CharSequence {
        val now = nowMinutes()
        val begin = minutesOf(s.begin)
        val end = minutesOf(s.end)
        return when {
            s.statusCode == CODE_FINISHED || now >= end ->
                if (compact) "已结束" else "已结束 · 今天辛苦了"
            now < begin -> if (compact) "${s.begin} 开始" else "未开始 · ${s.begin} 开始"
            else -> {
                val head = when {
                    compact -> ""
                    s.statusCode == 3141 -> "暂离中 · "
                    else -> "进行中 · "
                }
                val tail = "剩余 ${formatMinutes(end - now)}"
                SpannableString(head + tail).apply {
                    setSpan(
                        ForegroundColorSpan(ContextCompat.getColor(context, R.color.primary)),
                        head.length, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
                    )
                }
            }
        }
    }

    // endregion

    // region 本周节奏 Bitmap

    /**
     * 7 行「一二三四(今)(明)日」+ 时段条，画进 Bitmap。轨道范围 08:00–22:00，
     * 今天实心橙、明天蓝色虚线框、其余配置日半透明橙、休息日只剩轨道。
     */
    private fun weekBitmap(context: Context, s: ReservationCache.Snapshot, rowDp: Float): Bitmap {
        val density = context.resources.displayMetrics.density
        fun dp(v: Float) = v * density

        val width = dp(304f).toInt()
        val rowHeight = dp(rowDp)
        val bitmap = Bitmap.createBitmap(width, (rowHeight * 7).toInt(), Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)

        fun color(id: Int) = ContextCompat.getColor(context, id)
        val fill = Paint(Paint.ANTI_ALIAS_FLAG)
        val outline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = dp(1.2f)
            pathEffect = DashPathEffect(floatArrayOf(dp(3.5f), dp(2.5f)), 0f)
        }
        val label = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            textSize = dp(9.5f)
            typeface = Typeface.create("sans-serif", Typeface.BOLD)
        }
        val rect = RectF()

        val today = isoToday()
        val tomorrow = today % 7 + 1
        val trackLeft = dp(22f)
        val trackRight = width - dp(2f)
        val barHeight = minOf(rowHeight * 0.5f, dp(WEEK_BAR_MAX_DP))
        val radius = barHeight / 2f

        fun x(hour: Float) = trackLeft + ((hour - 8f) / 14f).coerceIn(0f, 1f) * (trackRight - trackLeft)

        for (iso in 1..7) {
            val centerY = (iso - 1) * rowHeight + rowHeight / 2f
            val segments = if (s.running) s.weekSegments(iso) else emptyList()

            label.color = when {
                iso == today -> color(R.color.primary)
                iso == tomorrow -> color(R.color.tomorrow)
                segments.isEmpty() -> color(R.color.stroke_muted)
                else -> color(R.color.text_muted)
            }
            val tag = when (iso) {
                today -> "今"
                tomorrow -> "明"
                else -> DAY_SHORT[iso - 1]
            }
            canvas.drawText(tag, 0f, centerY + dp(3.5f), label)

            // 全宽轨道垫底，配置时段叠在上面
            rect.set(trackLeft, centerY - barHeight / 2f, trackRight, centerY + barHeight / 2f)
            fill.color = color(R.color.surface_alt)
            canvas.drawRoundRect(rect, radius, radius, fill)

            segments.forEach { segment ->
                val from = minutesOf(segment.substringBefore('-')) / 60f
                val to = minutesOf(segment.substringAfter('-')) / 60f
                rect.set(x(from), centerY - barHeight / 2f, maxOf(x(to), x(from) + dp(4f)), centerY + barHeight / 2f)
                when (iso) {
                    today -> {
                        fill.color = color(R.color.primary)
                        canvas.drawRoundRect(rect, radius, radius, fill)
                    }
                    tomorrow -> {
                        fill.color = color(R.color.tomorrow_soft)
                        canvas.drawRoundRect(rect, radius, radius, fill)
                        outline.color = color(R.color.tomorrow)
                        canvas.drawRoundRect(rect, radius, radius, outline)
                    }
                    else -> {
                        fill.color = color(R.color.heat2)
                        canvas.drawRoundRect(rect, radius, radius, fill)
                    }
                }
            }
        }
        return bitmap
    }

    private fun weekSubtitle(s: ReservationCache.Snapshot): String {
        if (!s.running) return "自动预约已暂停"
        val configured = (1..7).map { s.weekSegments(it) }.filter { it.isNotEmpty() }
        if (configured.isEmpty()) return "本周全部休息"
        val uniform = configured.all { it.size == 1 && it.first() == configured.first().first() }
        return if (uniform && configured.size == 7) configured.first().first().replace("-", " – ")
        else "按星期配置"
    }

    // endregion

    // region 时间小工具

    private fun minutesOf(value: String): Int {
        val h = value.substringBefore(':').toIntOrNull() ?: return 0
        val m = value.substringAfter(':', "0").toIntOrNull() ?: 0
        return h * 60 + m
    }

    private fun nowMinutes(): Int = Calendar.getInstance().let {
        it.get(Calendar.HOUR_OF_DAY) * 60 + it.get(Calendar.MINUTE)
    }

    private fun isoToday(): Int = Calendar.getInstance().get(Calendar.DAY_OF_WEEK)
        .let { if (it == Calendar.SUNDAY) 7 else it - 1 }

    private fun formatMinutes(minutes: Int): String {
        val clamped = maxOf(minutes, 0)
        val h = clamped / 60
        val m = clamped % 60
        return when {
            h > 0 && m > 0 -> "${h}小时${m}分"
            h > 0 -> "${h}小时"
            else -> "${m}分钟"
        }
    }

    /** 窄列用的紧凑时长：整点「11小时」、半点「11.5小时」，其余才带分钟。 */
    private fun durationText(begin: String, end: String): String {
        val minutes = maxOf(minutesOf(end) - minutesOf(begin), 0)
        return when {
            minutes % 60 == 0 -> "${minutes / 60}小时"
            minutes % 60 == 30 -> "${minutes / 60}.5小时"
            else -> formatMinutes(minutes)
        }
    }

    // endregion

    /** 「我已到馆」在小组件里直接调 API，成功后回写缓存刷新三个组件。 */
    fun handleArrived(context: Context, pending: android.content.BroadcastReceiver.PendingResult) {
        val app = context.applicationContext
        Thread {
            try {
                val snapshot = ReservationCache.read(app)
                if (snapshot.pid.isBlank()) {
                    toast(app, "请先打开 App 同步数据")
                    return@Thread
                }
                val api = NativeApi(app)
                val response = api.postBlocking("/api/my/accounts/${api.encoded(snapshot.pid)}/arrived")
                if (response.ok) {
                    val arrived = response.jsonObject?.optBoolean("arrived") == true
                    ReservationCache.saveArrived(app, arrived)
                    toast(app, if (arrived) "已标记到馆，迟到保护今日不触发" else "已取消到馆标记")
                } else {
                    toast(app, response.message("操作失败，请打开 App 重试"))
                    refresh(app)
                }
            } catch (_: Exception) {
                toast(app, "网络异常，请打开 App 重试")
                refresh(app)
            } finally {
                pending.finish()
            }
        }.start()
    }

    private fun toast(context: Context, message: String) {
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }
}

abstract class BaseSeatWidgetProvider : AppWidgetProvider() {

    /** [minHeightDp] 是宿主分配的高度，0 表示拿不到（旧宿主或尚未回调）。 */
    abstract fun build(context: Context, minHeightDp: Int): RemoteViews

    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        ids.forEach { id -> manager.updateAppWidget(id, build(context, heightOf(context, manager, id))) }
    }

    /** 用户拉伸组件后重新按新高度出图。 */
    override fun onAppWidgetOptionsChanged(
        context: Context,
        manager: AppWidgetManager,
        id: Int,
        newOptions: android.os.Bundle?,
    ) {
        manager.updateAppWidget(id, build(context, heightOf(context, newOptions)))
    }

    private fun heightOf(context: Context, manager: AppWidgetManager, id: Int): Int =
        heightOf(context, manager.getAppWidgetOptions(id))

    /**
     * MIN_HEIGHT / MAX_HEIGHT 是两个屏幕方向下的高度界限，不是当前尺寸：
     * 竖屏时组件的实际高度是 MAX_HEIGHT，横屏才是 MIN_HEIGHT。
     */
    private fun heightOf(context: Context, options: android.os.Bundle?): Int {
        options ?: return 0
        val landscape = context.resources.configuration.orientation ==
            android.content.res.Configuration.ORIENTATION_LANDSCAPE
        return options.getInt(
            if (landscape) AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT
            else AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT
        )
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action == SeatWidgets.ACTION_ARRIVED) {
            SeatWidgets.handleArrived(context, goAsync())
        }
    }
}

/** SMALL 2×2 · 专注今天。 */
class SeatWidgetSmallProvider : BaseSeatWidgetProvider() {
    override fun build(context: Context, minHeightDp: Int): RemoteViews = SeatWidgets.buildSmall(context)
}

/** MEDIUM 4×2 · 今天 + 明日。沿用旧类名，老用户已放置的组件原地升级。 */
class SeatWidgetProvider : BaseSeatWidgetProvider() {
    override fun build(context: Context, minHeightDp: Int): RemoteViews = SeatWidgets.buildMedium(context)
}

/** LARGE 4×4 · 完整掌控一周。 */
class SeatWidgetLargeProvider : BaseSeatWidgetProvider() {
    override fun build(context: Context, minHeightDp: Int): RemoteViews =
        SeatWidgets.buildLarge(context, minHeightDp)
}
