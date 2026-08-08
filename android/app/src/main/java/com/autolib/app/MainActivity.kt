package com.autolib.app

import android.annotation.SuppressLint
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.content.ContextCompat
import androidx.core.view.isVisible
import com.autolib.app.databinding.ActivityMainBinding
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.switchmaterial.SwitchMaterial
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var api: NativeApi
    private var auth = JSONObject()
    private var accounts = JSONArray()
    private var seats = JSONObject()
    private var currentPid = ""
    private var currentConfig: JSONObject? = null
    private var napConfig = defaultNapConfig()
    private var todayReservation: JSONObject? = null
    private var tomorrowReservation: JSONObject? = null
    private var currentPage = PAGE_HOME
    private var tomorrowEditorOpen = false

    /** 配置页当前控件的取值器；离开配置页时为 null。 */
    private var collectConfigBody: (() -> JSONObject)? = null
    private var collectNapAuto: (() -> Boolean)? = null
    private var configEditable = false
    private val autosaveHandler = Handler(Looper.getMainLooper())
    private var autosaveRunnable: Runnable? = null

    /** 小组件按钮深链进来要打开的对话框，主页数据就绪后消费。 */
    private var pendingWidgetAction: String? = null

    /** 已自动补过到馆记录的日期，防止重复提交。 */
    private var autoArrivedFor: String? = null

    /**
     * 上次真正拉取预约的时刻与所属学号；查询会触发学校系统登录，很贵，所以要节流。
     * 带上 pid 是为了切换账号后缓存自动失效，不会把上一个学号的预约显示出来。
     */
    private var reservationsLoadedAt = 0L
    private var reservationsPid = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        AppCompatDelegate.setDefaultNightMode(nightModeOf(storedTheme()))
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        api = NativeApi(this)
        captureWidgetAction(intent)
        restoreState(savedInstanceState)
        setupNavigation()
        binding.accountButton.setOnClickListener { showAccountChooser() }
        // 先介绍再要权限：讲清楚「每天帮你抢座、结果推通知」之后再弹权限，
        // 用户才知道这个通知是干什么用的。两个对话框不能同时弹，所以串起来。
        showWelcomeIfNeeded { setupNotifications() }
        loadInitialData()
        // 每天至多一次，且只在确实有新版本时才弹
        UpdateChecker.checkSilently(this, api) { showUpdateDialog(it) }
    }

    /**
     * 首次启动的功能介绍。点「我知道啦」后永久关闭——key 带版本号，
     * 以后加了大功能把 v1 换成 v2，老用户会再看到一次新版介绍。
     *
     * 无论看没看过都会回调 [onDone]，调用方拿它串下一步（申请通知权限）。
     */
    private fun showWelcomeIfNeeded(onDone: () -> Unit) {
        val prefs = getPreferences(MODE_PRIVATE)
        if (prefs.getBoolean(PREF_WELCOME_ACK, false)) {
            onDone()
            return
        }

        val body = vertical(dp(4)).apply {
            addView(text("每天抢座太麻烦？配置一次，之后每天到点自动帮你抢好。", 13).apply {
                setTextColor(color(R.color.text_secondary))
                setPadding(dp(4), 0, dp(4), dp(10))
            })
            addView(noticeCard("📅 自动预约", WELCOME_RESERVE, R.color.primary))
            addView(noticeCard("🛡 迟到保护", WELCOME_LATE_PROTECTION, R.color.success))
            addView(noticeCard("😴 自动午休", WELCOME_NAP, R.color.tomorrow))
            addView(noticeCard("📊 小组件 & 热力图", WELCOME_WIDGET, R.color.text_muted))
        }

        AlertDialog.Builder(this)
            .setTitle("👋 欢迎使用 AutoLib")
            .setView(scrolled(body))
            .setPositiveButton("我知道啦") { _, _ ->
                prefs.edit().putBoolean(PREF_WELCOME_ACK, true).apply()
                onDone()
            }
            .setCancelable(false)
            .show()
    }

    /**
     * 抢座结果通知默认开启，所以启动时要补上排程（闹钟不跨重启也不跨安装），
     * 并在 Android 13+ 首次启动时申请一次通知权限。只申请一次——被拒之后
     * 再弹是骚扰，设置页的开关仍然可以手动打开。
     */
    private fun setupNotifications() {
        if (!ReservationAlarm.isEnabled(this)) return
        if (ReservationSync.hasPermission(this)) {
            ReservationSync.ensureChannel(this)
            ReservationAlarm.schedule(this)
            return
        }
        val prefs = getPreferences(MODE_PRIVATE)
        if (prefs.getBoolean(PREF_NOTIFY_ASKED, false)) return
        prefs.edit().putBoolean(PREF_NOTIFY_ASKED, true).apply()
        requestNotificationPermission()
    }

    override fun onNewIntent(intent: android.content.Intent?) {
        super.onNewIntent(intent)
        intent ?: return
        setIntent(intent)
        if (captureWidgetAction(intent)) {
            if (binding.navigation.selectedItemId != PAGE_HOME) binding.navigation.selectedItemId = PAGE_HOME
            else renderHome(refresh = true)
        }
    }

    private fun captureWidgetAction(intent: android.content.Intent?): Boolean {
        val action = intent?.action.orEmpty()
        if (!action.startsWith(SeatWidgets.ACTION_PREFIX)) return false
        pendingWidgetAction = action.removePrefix(SeatWidgets.ACTION_PREFIX)
        return true
    }

    private fun consumeWidgetAction() {
        val action = pendingWidgetAction ?: return
        pendingWidgetAction = null
        when (action) {
            "nap" -> todayReservation?.let { showNapDialog(it) } ?: toast("今日暂无预约")
            "cancel" -> todayReservation?.let { confirmCancel(it) } ?: toast("今日暂无预约")
            "reserve" -> showReserveDialog()
            // "open" / "tomorrow" 不需要额外弹窗
        }
    }

    override fun onPause() {
        flushConfigAutosave()
        super.onPause()
    }

    /**
     * 切换主题会让 setDefaultNightMode 重建 Activity。不接管状态的话有两个后果：
     * 停留的页签回到主页（而 BottomNavigationView 自己恢复了旧选中项，两者对不上），
     * 以及预约缓存丢失、白白再触发一次昂贵的学校系统查询。
     */
    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt(STATE_PAGE, currentPage)
        outState.putLong(STATE_RESERVATIONS_AT, reservationsLoadedAt)
        outState.putString(STATE_RESERVATIONS_PID, reservationsPid)
        todayReservation?.let { outState.putString(STATE_TODAY, it.toString()) }
        tomorrowReservation?.let { outState.putString(STATE_TOMORROW, it.toString()) }
    }

    private fun restoreState(saved: Bundle?) {
        saved ?: return
        currentPage = saved.getInt(STATE_PAGE, PAGE_HOME)
        reservationsLoadedAt = saved.getLong(STATE_RESERVATIONS_AT, 0L)
        reservationsPid = saved.getString(STATE_RESERVATIONS_PID).orEmpty()
        todayReservation = saved.getString(STATE_TODAY)?.let { runCatching { JSONObject(it) }.getOrNull() }
        tomorrowReservation = saved.getString(STATE_TOMORROW)?.let { runCatching { JSONObject(it) }.getOrNull() }
    }

    private fun setupNavigation() {
        binding.navigation.menu.apply {
            add(0, PAGE_HOME, 0, "主页").setIcon(R.drawable.ic_home_native)
            add(0, PAGE_CONFIG, 1, "配置").setIcon(R.drawable.ic_config_native)
            add(0, PAGE_SETTINGS, 2, "设置").setIcon(R.drawable.ic_settings_native)
        }
        // 选中项由 currentPage 单独接管（见 onSaveInstanceState），
        // 关掉 View 自带的状态恢复，否则它会在 onCreate 之后把选中项改回去。
        binding.navigation.isSaveEnabled = false
        binding.navigation.setOnItemSelectedListener { item ->
            flushConfigAutosave()
            currentPage = item.itemId
            // 切页不强刷：renderHome 会在复用窗口内直接用上次的结果，
            // 想要最新的用标题栏的刷新按钮。
            renderCurrentPage()
            true
        }
        binding.navigation.selectedItemId = currentPage
    }

    private fun loadInitialData() {
        setBusy(true)
        api.get("/api/auth/me") { me ->
            auth = me.jsonObject ?: JSONObject()
            api.get("/api/seats") { seatResponse ->
                seats = seatResponse.jsonObject?.optJSONObject("seats") ?: JSONObject()
                loadAccounts()
            }
        }
    }

    private fun loadAccounts() {
        api.get("/api/my/accounts") { response ->
            accounts = response.jsonArray ?: JSONArray()
            val saved = getPreferences(MODE_PRIVATE).getString("current_pid", "")
            currentPid = when {
                currentPid.isNotBlank() && accountExists(currentPid) -> currentPid
                !saved.isNullOrBlank() && accountExists(saved) -> saved
                accounts.length() > 0 -> accounts.optJSONObject(0)?.optString("pid").orEmpty()
                else -> ""
            }
            if (currentPid.isBlank()) {
                currentConfig = null
                napConfig = defaultNapConfig()
                setBusy(false)
                updateHeader()
                renderCurrentPage()
            } else {
                loadAccountDetail(currentPid)
            }
        }
    }

    private fun loadAccountDetail(pid: String, after: (() -> Unit)? = null) {
        api.get("/api/my/accounts/${api.encoded(pid)}") { response ->
            currentConfig = response.jsonObject?.takeIf { it.optString("pid").isNotBlank() }
            getPreferences(MODE_PRIVATE).edit().putString("current_pid", pid).apply()
            api.get("/api/my/accounts/${api.encoded(pid)}/nap_config") { napResponse ->
                napConfig = napResponse.jsonObject?.takeIf { napResponse.ok } ?: defaultNapConfig()
                setBusy(false)
                updateHeader()
                if (after != null) after() else renderCurrentPage(refresh = currentPage == PAGE_HOME)
            }
        }
    }

    private fun renderCurrentPage(refresh: Boolean = false) {
        when (currentPage) {
            PAGE_CONFIG -> renderConfig()
            PAGE_SETTINGS -> renderSettings()
            else -> renderHome(refresh)
        }
    }

    private fun loggedIn() = auth.optBoolean("logged_in")
    private fun displayName() = auth.optString("nickname").ifBlank { auth.optString("uid") }

    private fun updateHeader() {
        binding.title.text = "您好，${if (loggedIn()) displayName() else "同学"}☕"
        binding.subtitle.text = SimpleDateFormat("EEE · MM月dd日", Locale.CHINA).format(Date())
        binding.accountButton.text = when {
            currentPid.isBlank() -> "＋ 添加学号"
            accountSummary(currentPid)?.optString("is_reserved") == "True" -> "● $currentPid"
            else -> "○ $currentPid"
        }
    }

    // region Home
    private fun renderHome(refresh: Boolean) {
        val host = pageHost()
        val cfg = currentConfig
        cacheWidgetPlan(cfg)
        // 「调整明日」深链要在卡片构建前展开编辑器
        if (pendingWidgetAction == "tomorrow") {
            pendingWidgetAction = null
            tomorrowEditorOpen = true
        }
        host.addView(sectionRow("今日预约", refreshButton()))
        val todayCard = card().also { styleHeroCard(it, tomorrow = false); host.addView(it) }
        val tomorrowCard = card().also { styleHeroCard(it, tomorrow = true) }
        if (cfg != null) {
            host.addView(section("明日预约"))
            host.addView(tomorrowCard)
            host.addView(section("调整明日"))
            host.addView(tomorrowEditorCard(cfg))
            host.addView(section("学习记录"))
            visitStatsCard(host)
        }
        host.addView(section("本周 · 配置预览"))
        host.addView(weekPreviewCard(cfg))
        host.addView(section("通知"))
        val notices = vertical().also { host.addView(it) }

        if (cfg == null) {
            replaceCard(todayCard, vertical(0).apply {
                addView(heroHeader(false))
                addView(text("还没有学号", 22, true))
                addView(text("添加学号后系统会自动抢座。", 14))
                addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() })
            })
            loadNotices(notices)
            consumeWidgetAction()
            return
        }

        // 未验证的学号无法登录学校系统，查询只会白白触发一次昂贵的失败登录。
        if (!cfg.optBoolean("verified")) {
            todayReservation = null
            tomorrowReservation = null
            renderReservationCard(todayCard, null, tomorrow = false, unverified = true)
            renderReservationCard(tomorrowCard, null, tomorrow = true, unverified = true)
            loadNotices(notices)
            pendingWidgetAction = null
            return
        }

        // 预约查询要走学校系统登录，很慢也很贵。窗口内直接复用上次的结果，
        // 想看最新的用标题栏的刷新按钮。
        val fresh = reservationsPid == currentPid &&
            System.currentTimeMillis() - reservationsLoadedAt < RESERVATION_TTL_MS
        if (!refresh && fresh && reservationsLoadedAt > 0L) {
            renderReservationCard(todayCard, todayReservation, false)
            renderReservationCard(tomorrowCard, tomorrowReservation, true)
            loadNotices(notices)
            consumeWidgetAction()
            return
        }

        showReservationLoading(todayCard, "正在查询今日预约…")
        showReservationLoading(tomorrowCard, "正在查询明日预约…")
        if (refresh) setBusy(true)
        api.get("/api/my/accounts/${api.encoded(currentPid)}/reservations") { response ->
            setBusy(false)
            if (!response.ok) {
                replaceCard(todayCard, text(response.message("预约查询失败"), 14))
                replaceCard(tomorrowCard, text("无法取得预约数据", 14))
            } else {
                val list = response.jsonObject?.optJSONArray("reservations") ?: JSONArray()
                todayReservation = findReservation(list, dayOffset = 0)
                tomorrowReservation = findReservation(list, dayOffset = 1)
                reservationsLoadedAt = System.currentTimeMillis()
                reservationsPid = currentPid
                renderReservationCard(todayCard, todayReservation, false)
                renderReservationCard(tomorrowCard, tomorrowReservation, true)
                // 顺手更新桌面小组件的数据源
                ReservationCache.saveReservations(
                    this, currentPid, todayReservation,
                    todayReservation?.let { reservationStatus(it.optInt("resvStatus", -1)) }.orEmpty(),
                    tomorrowReservation,
                )
            }
            consumeWidgetAction()
        }
        loadNotices(notices)
    }

    /** 把小组件要用的周计划、到馆与保护状态写进缓存。 */
    private fun cacheWidgetPlan(cfg: JSONObject?) {
        if (cfg == null) {
            // cfg 为 null 有两种原因：确实没有学号，或者详情没拉到（网络异常）。
            // 后者不能动缓存——桌面上原本正确的数据会被抹掉。
            if (accounts.length() == 0) ReservationCache.clear(this)
            return
        }
        val week = JSONObject()
        (1..7).forEach { iso -> week.put(iso.toString(), JSONArray(effectiveSegments(cfg, iso))) }
        ReservationCache.savePlan(
            this,
            running = cfg.optString("is_reserved") == "True",
            mode = cfg.optString("mode").orEmpty().ifBlank { "week_time" },
            weekJson = week.toString(),
            arrivedDate = cfg.optString("arrived_date"),
            lateProtection = cfg.optString("late_protection") == "True",
        )
    }

    private fun renderReservationCard(
        target: MaterialCardView,
        reservation: JSONObject?,
        tomorrow: Boolean,
        unverified: Boolean = false,
    ) {
        target.removeAllViews()
        val content = vertical(dp(18))
        target.addView(content)
        content.addView(heroHeader(tomorrow))
        val cfg = currentConfig
        val running = cfg?.optString("is_reserved") == "True"

        if (reservation == null) {
            content.addView(text(if (tomorrow) "明日暂无预约" else "今日暂无预约", 19, true))
            content.addView(text(when {
                unverified -> "学号尚未验证，去配置页验证后才能查询预约"
                tomorrow && running -> "将按配置自动预约，结果会在抢座后可见"
                tomorrow -> "自动预约已暂停，去配置页开启"
                running -> "按当前配置今日无座位，或抢座进行中"
                else -> "自动预约已暂停"
            }, 14))
            if (!tomorrow && !unverified && napConfig.optBoolean("auto_daily")) {
                content.addView(pillRow(statusPill(
                    "😴 自动午休 ${napConfig.optString("trigger_time").ifBlank { "12:00" }}",
                    R.color.primary, R.color.accent_soft,
                )))
            }
            if (unverified) {
                if (!tomorrow) content.addView(action("去验证", accent = true) {
                    binding.navigation.selectedItemId = PAGE_CONFIG
                })
            } else if (tomorrow) {
                // 取消明日预约后要能在原地重订
                content.addView(action("⚡ 预约明日", accent = true) { showReserveDialog(tomorrow = true) })
            } else {
                content.addView(action("⚡ 立即预约", accent = true) { showReserveDialog() })
            }
            return
        }

        val seat = reservation.optJSONObject("devInfo")?.optString("devName").orEmpty().ifBlank { "未知座位" }
        val begin = reservation.optString("resvBeginTime").substringAfter(' ', "").take(5)
        val end = reservation.optString("resvEndTime").substringAfter(' ', "").take(5)
        val code = reservation.optInt("resvStatus", -1)
        content.addView(text(seat, 38, true).apply {
            setTextColor(color(if (tomorrow) R.color.tomorrow else R.color.primary))
            typeface = Typeface.create("sans-serif-rounded", Typeface.BOLD)
            letterSpacing = -0.025f
        })
        content.addView(text("$begin  —  $end", 18, true).apply { setTextColor(color(R.color.text_secondary)) })

        if (!tomorrow && code == STATUS_FINISHED) {
            content.addView(text("任务完成，该休息了 ☕", 18, true))
            content.addView(text("今日学习已结束", 14))
            content.addView(action("我还能学！", accent = true) { showReserveDialog() })
            return
        }
        if (!tomorrow && code in BREACHED_STATUSES) {
            content.addView(pillRow(statusPill("⚠ 今日预约已违约", R.color.danger, R.color.danger_soft)))
            content.addView(action("⚡ 再次预约", accent = true) { showReserveDialog(seat) })
            return
        }

        val arrived = cfg?.optString("arrived_date") == todayStamp()
        val showArrived = arrived || code == STATUS_IN_USE || code == STATUS_AWAY
        if (!tomorrow && !arrived && (code == STATUS_IN_USE || code == STATUS_AWAY)) {
            // 图书馆已经报"使用中/暂离"，说明人确实刷卡入座了，把到馆记录补上，
            // 免得迟到保护在后台误判成没来。
            autoMarkArrived()
        }
        content.addView(pillRow(
            statusPill(reservationStatus(code),
                if (tomorrow) R.color.tomorrow else R.color.success,
                if (tomorrow) R.color.tomorrow_soft else R.color.success_soft),
            if (!tomorrow && cfg?.optString("late_protection") == "True")
                statusPill("🛡 迟到保护", R.color.primary, R.color.accent_soft) else null,
            if (!tomorrow && showArrived)
                statusPill("✓ 已到馆", R.color.success, R.color.success_soft) else null,
            // 取消明日不是这张卡的重点，做成跟在状态徽章后面的小药丸而不是整行按钮
            if (tomorrow && code in ACTIVE_STATUSES)
                statusPill("取消", R.color.danger, R.color.danger_soft).apply {
                    setOnClickListener { confirmCancel(reservation, tomorrow = true) }
                } else null,
        ))
        if (!tomorrow && code in ACTIVE_STATUSES) {
            val row = horizontal()
            // 到馆按钮文字最长，多分一点宽度，否则"已到馆"会被挤到第二行
            row.addView(action(if (showArrived) "✓ 已到馆" else "✓ 我已到馆", 1.5f, accent = !showArrived) { toggleArrived() })
            row.addView(action("午休", 1f) { showNapDialog(reservation) })
            // 已经入座的话释放座位是"离馆"；还没到馆才是撤销这次预约
            row.addView(action(if (showArrived) "离馆" else "取消", 1f, danger = true) {
                confirmCancel(reservation, leaving = showArrived)
            })
            content.addView(row)
        }
    }

    /** 「调整明日」快捷编辑，对应网页端的 tmr-strip / tmr-body。 */
    private fun tomorrowEditorCard(cfg: JSONObject): MaterialCardView {
        val card = card()
        val content = vertical(dp(16))
        card.addView(content)

        val calendar = Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, 1) }
        val iso = isoOf(calendar)
        val running = cfg.optString("is_reserved") == "True"
        val seatList = jsonArrayStrings(cfg.optJSONArray("seat_list"))
        val segments = effectiveSegments(cfg, iso)

        val caret = text(if (tomorrowEditorOpen) "▾" else "▸", 18, true)
        val header = horizontal()
        header.addView(vertical(0).apply {
            addView(text(WEEK_SHORT[iso - 1], 10, true).apply {
                setTextColor(color(R.color.text_muted)); gravity = Gravity.CENTER; letterSpacing = 0.08f
            })
            addView(text(calendar.get(Calendar.DAY_OF_MONTH).toString(), 22, true).apply { gravity = Gravity.CENTER })
        }, LinearLayout.LayoutParams(dp(46), ViewGroup.LayoutParams.WRAP_CONTENT))
        header.addView(vertical(0).apply {
            addView(text(if (running) "按配置自动预约" else "自动预约已暂停", 16, true))
            addView(text(
                if (running) "明早抢 ${seatList.firstOrNull() ?: "—"} · ${segmentLabel(segments)}"
                else "去配置页开启「自动预约」",
                13,
            ).apply { setTextColor(color(R.color.text_secondary)) })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        header.addView(caret)
        content.addView(header)

        val editor = vertical(0).apply { isVisible = tomorrowEditorOpen }
        content.addView(editor)
        editor.addView(text("仅调整明日（会切换到「统一时段」模式）", 12)
            .apply { setTextColor(color(R.color.text_muted)) })
        val max = dayMax(iso)
        val first = segments.firstOrNull() ?: "08:00-22:00"
        val (s, e) = clampRange(first.substringBefore('-'), first.substringAfter('-'), iso)
        val start = spinner(timeOptions("08:00", max)).apply { selectValue(s) }
        val end = spinner(timeOptions("08:00", max)).apply { selectValue(e) }
        editor.addView(horizontal().apply {
            addView(labeled("开始", start), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(text("—", 14).apply { gravity = Gravity.CENTER },
                LinearLayout.LayoutParams(dp(24), ViewGroup.LayoutParams.WRAP_CONTENT))
            addView(labeled("结束", end), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        })
        if (segments.size > 1) {
            editor.addView(text("⚠ 当前有 ${segments.size} 段，快速编辑仅保留首段。多段请在配置页调整", 12)
                .apply { setTextColor(color(R.color.primary_variant)) })
        }
        editor.addView(text("座位优先级（来自配置）", 11, true)
            .apply { setTextColor(color(R.color.text_muted)); letterSpacing = 0.08f })
        editor.addView(text(
            if (seatList.isEmpty()) "还没有配置座位"
            else seatList.mapIndexed { index, name -> "${index + 1}. $name" }.joinToString("   "),
            13,
        ))
        editor.addView(horizontal().apply {
            addView(action("收起", 1f) {
                tomorrowEditorOpen = false
                editor.isVisible = false
                caret.text = "▸"
            })
            addView(action("保存明日", 1f, accent = true) {
                saveTomorrow(cfg, start.selectedItem.toString(), end.selectedItem.toString(), iso)
            })
        })

        header.setOnClickListener {
            tomorrowEditorOpen = !tomorrowEditorOpen
            editor.isVisible = tomorrowEditorOpen
            caret.text = if (tomorrowEditorOpen) "▾" else "▸"
        }
        return card
    }

    private fun saveTomorrow(cfg: JSONObject, rawStart: String, rawEnd: String, iso: Int) {
        val (s, e) = clampRange(rawStart, rawEnd, iso)
        if (s >= e) return toast("时间无效")
        // 保留 week_time，只覆盖 tomorrow，和网页端 saveTmr 一致。
        val time = JSONObject()
        cfg.optJSONObject("time")?.let { existing ->
            existing.keys().forEach { key -> time.put(key, existing.get(key)) }
        }
        time.put("tomorrow", JSONArray().put("$s-$e"))
        setBusy(true)
        api.post("/api/my/accounts/${api.encoded(currentPid)}", JSONObject()
            .put("mode", "tomorrow")
            .put("time", time)
            .put("seat_list", cfg.optJSONArray("seat_list") ?: JSONArray())) { response ->
            setBusy(false)
            if (!response.ok) return@post toast(response.message("保存失败"))
            toast("明日已更新（已切换到统一时段模式）")
            tomorrowEditorOpen = false
            loadAccounts()
        }
    }

    /** 「本周 · 配置预览」，对应网页端的 renderWeekPreview()。 */
    private fun weekPreviewCard(cfg: JSONObject?): MaterialCardView {
        val card = card()
        val content = vertical(dp(16))
        card.addView(content)
        val running = cfg?.optString("is_reserved") == "True"
        val mode = cfg?.optString("mode").orEmpty().ifBlank { "week_time" }
        content.addView(horizontal().apply {
            addView(text("我的每周节奏", 15, true),
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(text(when {
                cfg == null -> "08 — 22 时"
                !running -> "自动预约已暂停"
                mode == "tomorrow" -> "统一时段模式"
                else -> "按星期模式"
            }, 12).apply { setTextColor(color(R.color.text_muted)) })
        })

        val today = isoToday()
        val tomorrow = if (today == 7) 1 else today + 1
        val preview = WeekPreviewView(this)
        preview.days = (1..7).map { iso ->
            val segments = if (cfg != null && running) effectiveSegments(cfg, iso) else emptyList()
            val tone = when {
                segments.isEmpty() -> WeekPreviewView.Tone.OFF
                mode == "tomorrow" || iso == today -> WeekPreviewView.Tone.TODAY
                iso == tomorrow -> WeekPreviewView.Tone.TOMORROW
                else -> WeekPreviewView.Tone.ACTIVE
            }
            WeekPreviewView.Day(
                label = "周${DAY_SHORT[iso - 1]}" + if (segments.size > 1) " ×${segments.size}" else "",
                tag = when (iso) {
                    today -> "今"
                    tomorrow -> "明"
                    else -> null
                },
                segments = segments.map(::hourRange),
                tone = tone,
                showTimeLabel = iso == today || iso == tomorrow || mode == "tomorrow",
                labelTone = if (iso == today) WeekPreviewView.Tone.TODAY else tone,
            )
        }
        content.addView(preview, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        content.addView(text(when {
            cfg == null -> "💡 添加学号后查看配置预览"
            !running -> "💡 开启自动预约后这里会亮起"
            mode == "tomorrow" -> "💡 每天都按同一时段预约"
            else -> "💡 每天按配置的时段预约"
        }, 12).apply { setTextColor(color(R.color.text_muted)) })
        return card
    }

    private fun loadNotices(host: LinearLayout) {
        host.removeAllViews()
        host.addView(text("加载中…", 14))
        api.get("/api/announcements") { announcementsResponse ->
            api.get("/api/my/reservation_results") { resultsResponse ->
                host.removeAllViews()
                val announcements = announcementsResponse.jsonArray ?: JSONArray()
                val results = resultsResponse.jsonArray ?: JSONArray()
                for (i in 0 until announcements.length()) {
                    val item = announcements.optJSONObject(i) ?: continue
                    val title = buildString {
                        append("公告 · ")
                        append(item.optString("title"))
                        if (item.optBoolean("pinned")) append("  [置顶]")
                    }
                    host.addView(noticeCard(title, item.optString("content"), announcementColor(item.optString("level"))))
                }
                for (i in 0 until results.length()) {
                    val item = results.optJSONObject(i) ?: continue
                    val success = item.optBoolean("success")
                    host.addView(noticeCard(
                        "学号 ${item.optString("pid")} · ${if (success) "预约成功" else "预约失败"}",
                        item.optString("result"),
                        if (success) R.color.success else R.color.danger,
                    ))
                }
                if (host.childCount == 0) host.addView(text("暂无通知", 14))
            }
        }
    }

    private fun showReserveDialog(defaultSeat: String? = null, tomorrow: Boolean = false) {
        if (currentPid.isBlank()) return showAddAccountDialog()
        if (seats.length() == 0) return toast("座位列表尚未加载")
        val body = vertical()
        val zone = spinner(sortedZones())
        val seat = spinner(emptyList())
        bindZoneToSeats(zone, seat)
        // 闭馆时间按目标那天算——周五收得早，明日预约不能套用今天的上限
        val iso = if (tomorrow) isoToday() % 7 + 1 else isoToday()
        val max = dayMax(iso)
        val start = spinner(timeOptions("08:00", max))
        val end = spinner(timeOptions("10:00", max)).apply { setSelection(adapter.count - 1) }
        val favourites = (currentConfig?.optJSONArray("seat_list")?.let { jsonArrayStrings(it) } ?: emptyList()).take(3)
        if (favourites.isNotEmpty()) {
            body.addView(text("常用座位 · 点击快速选择", 12, true).apply { setTextColor(color(R.color.text_muted)) })
            val chips = horizontal()
            favourites.forEach { name -> chips.addView(action(name, compact = true) { selectSeat(zone, seat, name) }) }
            body.addView(chips)
        }
        body.addView(labeled("区域", zone)); body.addView(labeled("座位", seat))
        body.addView(labeled("开始时间", start)); body.addView(labeled("结束时间", end))
        val dialog = AlertDialog.Builder(this)
            .setTitle(if (tomorrow) "预约明日座位" else "立即预约今日座位")
            .setView(scrolled(body))
            .setNegativeButton("取消", null).setPositiveButton("预约", null).create()
        dialog.setOnShowListener {
            (defaultSeat ?: favourites.firstOrNull())?.let { name -> selectSeat(zone, seat, name) }
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val seatValue = seat.selectedItem?.toString().orEmpty()
                val startValue = start.selectedItem?.toString().orEmpty()
                val endValue = end.selectedItem?.toString().orEmpty()
                if (seatValue.isBlank()) return@setOnClickListener toast("请选择座位")
                if (startValue >= endValue || minutesBetween(startValue, endValue) < 120) {
                    return@setOnClickListener toast("结束时间须晚于开始时间，且至少预约 2 小时")
                }
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                setBusy(true)
                api.post("/api/my/accounts/${api.encoded(currentPid)}/reserve_custom", JSONObject()
                    .put("seat", seatValue)
                    .put("start_time", startValue).put("end_time", endValue)
                    .put("day", if (tomorrow) "tomorrow" else "today")) { response ->
                    setBusy(false)
                    val result = response.jsonObject?.optString("result").orEmpty()
                    if (response.ok) {
                        dialog.dismiss()
                        showReserveResult(result.ifBlank { "预约完成" }, response.jsonObject?.optBoolean("success") == true)
                        renderHome(refresh = true)
                    } else {
                        toast(response.message("预约失败"))
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                    }
                }
            }
        }
        dialog.show()
    }

    private fun showReserveResult(message: String, success: Boolean) {
        AlertDialog.Builder(this)
            .setTitle(if (success) "✅ 预约成功" else "❌ 预约失败")
            .setMessage(message)
            .setPositiveButton(if (success) "好的" else "知道了", null)
            .show()
    }

    private fun confirmCancel(
        reservation: JSONObject,
        leaving: Boolean = false,
        tomorrow: Boolean = false,
    ) {
        AlertDialog.Builder(this)
            .setTitle(when {
                tomorrow -> "取消明日预约？"
                leaving -> "确认离馆？"
                else -> "取消今日预约？"
            })
            .setMessage(when {
                tomorrow -> "取消后座位立即释放，可能被他人抢走。取消完可以在原位置重新预约明天。"
                leaving -> "离馆后座位立即释放，今天不能再用这次预约。临时外出请用「午休」保留座位。"
                else -> "取消后座位会释放。如果只是临时外出，可以不用取消。"
            })
            .setNegativeButton("再想想", null)
            .setPositiveButton(if (leaving) "确认离馆" else "确认取消") { _, _ ->
                setBusy(true)
                api.post("/api/my/accounts/${api.encoded(currentPid)}/cancel", JSONObject().put("uuid", reservation.optString("uuid"))) { response ->
                    setBusy(false)
                    toast(response.jsonObject?.optString("message").orEmpty().ifBlank { response.message("取消请求已完成") })
                    renderHome(refresh = true)
                }
            }.show()
    }

    /**
     * 依据图书馆返回的座位状态补一条到馆记录。
     *
     * `/arrived` 是 toggle 语义（已是今天就清空），所以只在确认当前**不是**已到馆时才调，
     * 否则会把状态反向清掉。[autoArrivedFor] 防止渲染重入时重复提交——请求在飞的
     * 期间 currentConfig 还没更新，条件仍然成立。
     */
    private fun autoMarkArrived() {
        val today = todayStamp()
        if (autoArrivedFor == today || currentPid.isBlank()) return
        autoArrivedFor = today
        api.post("/api/my/accounts/${api.encoded(currentPid)}/arrived") { response ->
            if (response.ok && response.jsonObject?.optBoolean("arrived") == true) {
                currentConfig?.put("arrived_date", today)
                ReservationCache.saveArrived(this, true)
                renderCurrentPage()
            } else {
                // 没写成就让下次渲染再试
                autoArrivedFor = null
            }
        }
    }

    private fun toggleArrived() {
        setBusy(true)
        api.post("/api/my/accounts/${api.encoded(currentPid)}/arrived") { response ->
            setBusy(false)
            if (!response.ok) return@post toast(response.message("操作失败"))
            toast(if (response.jsonObject?.optBoolean("arrived") == true) "已标记到馆，迟到保护今日不触发" else "已取消到馆标记")
            loadAccountDetail(currentPid) { renderHome(refresh = true) }
        }
    }

    private fun showNapDialog(reservation: JSONObject) {
        val currentSeat = reservation.optJSONObject("devInfo")?.optString("devName").orEmpty()
        val end = reservation.optString("resvEndTime").substringAfter(' ', "").take(5)
        val body = vertical()
        body.addView(text("系统会先取消当前预约，再预约同一座位的下午时段。取消到重新预约约需 1 秒，极低概率被他人抢占。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        body.addView(text("当前座位：${currentSeat.ifBlank { "未知" }}", 14, true))
        val back = spinner(timeOptions("08:00", dayMax(isoToday())))
            .apply { selectValue(napConfig.optString("start_time").ifBlank { "14:00" }) }
        body.addView(labeled("午休结束（回来时间）", back))
        val savedSeat = napConfig.optString("seat")
        val seatMode = spinner(listOf("当前座位（${currentSeat.ifBlank { "自动" }}）", "自定义座位"))
            .apply { setSelection(if (savedSeat.isNotBlank()) 1 else 0) }
        body.addView(labeled("座位", seatMode))
        val zone = spinner(sortedZones())
        val seatPick = spinner(emptyList())
        bindZoneToSeats(zone, seatPick)
        val customBlock = vertical(0).apply {
            addView(labeled("楼层 / 区域", zone)); addView(labeled("座位号", seatPick))
            isVisible = savedSeat.isNotBlank()
        }
        if (savedSeat.isNotBlank()) selectSeat(zone, seatPick, savedSeat)
        seatMode.onItemSelectedListener = simpleSelection { customBlock.isVisible = seatMode.selectedItemPosition == 1 }
        body.addView(customBlock)

        fun chosenSeat(): String =
            if (seatMode.selectedItemPosition == 1) seatPick.selectedItem?.toString().orEmpty() else currentSeat

        AlertDialog.Builder(this).setTitle("😴 午休设置").setView(scrolled(body))
            .setNeutralButton("仅保存", null)
            .setNegativeButton("取消", null)
            .setPositiveButton("确认午休", null)
            .create().apply {
                setOnShowListener {
                    getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                        saveNapConfig(copyNapConfig()
                            .put("start_time", back.selectedItem.toString())
                            .put("end_time", "")
                            .put("seat", if (seatMode.selectedItemPosition == 1) chosenSeat() else "")) { dismiss() }
                    }
                    getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                        val backValue = back.selectedItem.toString()
                        val seatValue = chosenSeat()
                        if (backValue >= end) return@setOnClickListener toast("回来时间必须早于当前预约结束时间")
                        if (seatValue.isBlank()) return@setOnClickListener toast("无法获取座位信息")
                        dismiss()
                        setBusy(true)
                        api.post("/api/my/accounts/${api.encoded(currentPid)}/nap", JSONObject()
                            .put("uuid", reservation.optString("uuid")).put("seat", seatValue)
                            .put("start_time", backValue).put("end_time", end)) { response ->
                            setBusy(false)
                            val payload = response.jsonObject
                            when {
                                !response.ok -> toast(response.message("午休失败"))
                                payload?.optBoolean("cancel_success") != true -> toast(response.message("取消失败"))
                                payload.optBoolean("success") -> toast("午休成功 😴 下午见！")
                                else -> showReserveResult(payload.optString("result").ifBlank { "重新预约失败，请手动预约下午时段" }, false)
                            }
                            renderHome(refresh = true)
                        }
                    }
                }
            }.show()
    }

    private fun saveNapConfig(config: JSONObject, after: (() -> Unit)? = null) {
        if (currentPid.isBlank()) return
        api.post("/api/my/accounts/${api.encoded(currentPid)}/nap_config", config) { response ->
            if (response.ok) {
                napConfig = config
                toast("午休配置已保存")
                after?.invoke()
            } else toast(response.message("保存失败"))
        }
    }
    // endregion

    // region Config
    private class DayRow(val enabled: SwitchMaterial, val segments: LinearLayout)

    private fun renderConfig() {
        val host = pageHost()
        host.addView(section("每日预约规则"))
        val cfg = currentConfig
        if (cfg == null) {
            host.addView(card().apply {
                addView(vertical(dp(18)).apply {
                    addView(text("还没有学号", 20, true))
                    addView(text("添加一个学号后才能配置预约规则。", 14))
                    addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() })
                })
            })
            return
        }

        val headerBody = vertical(0)
        headerBody.addView(horizontal().apply {
            addView(text(cfg.optString("pid"), 20, true), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(if (cfg.optBoolean("verified")) statusPill("✓ 已验证", R.color.success, R.color.success_soft)
            else statusPill("⚠ 未验证", R.color.danger, R.color.danger_soft))
        })
        headerBody.addView(text("配置会在每天定时自动抢座。修改保存后下一个抢座窗口生效。", 13)
            .apply { setTextColor(color(R.color.text_muted)) })
        host.addView(card().apply { addView(vertical(dp(18)).apply { addView(headerBody) }) })

        // ---- 时间模式 ----
        val modeGroup = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val weekRadio = RadioButton(this).apply { text = "按星期"; id = View.generateViewId() }
        val simpleRadio = RadioButton(this).apply { text = "统一时段"; id = View.generateViewId() }
        modeGroup.addView(weekRadio); modeGroup.addView(simpleRadio)
        val isWeek = cfg.optString("mode", "week_time") == "week_time"
        modeGroup.check(if (isWeek) weekRadio.id else simpleRadio.id)
        host.addView(cardBlock("时间模式", modeGroup))

        // 两种模式的控件都常驻，仅切换可见性，避免来回切换时丢掉未保存的编辑。
        val weekJson = cfg.optJSONObject("time")?.optJSONObject("week_time")
        val weekBlock = vertical(0)
        val dayRows = mutableListOf<DayRow>()
        DAY_LABELS.forEachIndexed { index, label ->
            val day = index + 1
            val raw = weekJson?.opt(day.toString())
            val isOff = raw == "休息" || raw == "off"
            val segments = valueSegments(raw).ifEmpty { listOf(WEEK_DEFAULTS[index]) }
            val segmentList = vertical(0)
            val enabled = SwitchMaterial(this).apply {
                text = label + (if (day == 5) "（最晚 20:00）" else "")
                isChecked = !isOff
            }
            val addButton = action("＋ 加时段", compact = true) {
                segmentList.addView(segmentRow(WEEK_DEFAULTS[day - 1], day, true) { row ->
                    removeSegment(segmentList, row, "至少保留一段，关闭当天请用左侧开关")
                })
                scheduleConfigAutosave()
            }
            weekBlock.addView(horizontal().apply {
                addView(enabled, LinearLayout.LayoutParams(0, dp(52), 1f))
                addView(addButton)
            })
            segments.forEach { segment ->
                segmentList.addView(segmentRow(segment, day, !isOff) { row ->
                    removeSegment(segmentList, row, "至少保留一段，关闭当天请用左侧开关")
                })
            }
            weekBlock.addView(segmentList)
            fun sync() {
                setSegmentsEnabled(segmentList, enabled.isChecked)
                addButton.isEnabled = enabled.isChecked
            }
            enabled.setOnCheckedChangeListener { _, _ -> sync(); scheduleConfigAutosave() }
            sync()
            dayRows += DayRow(enabled, segmentList)
        }

        val simpleBlock = vertical(0)
        val simpleList = vertical(0)
        val simpleSegments = valueSegments(cfg.optJSONObject("time")?.opt("tomorrow")).ifEmpty { listOf("08:00-22:00") }
        simpleSegments.forEach { segment ->
            simpleList.addView(segmentRow(segment, null, true) { row -> removeSegment(simpleList, row, "至少保留一段") })
        }
        simpleBlock.addView(horizontal().apply {
            addView(text("每日固定时段", 14, true), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(action("＋ 加时段", compact = true) {
                simpleList.addView(segmentRow("08:00-22:00", null, true) { row -> removeSegment(simpleList, row, "至少保留一段") })
                scheduleConfigAutosave()
            })
        })
        simpleBlock.addView(simpleList)
        simpleBlock.addView(text("💡 周五实际抢座会自动缩到 20:00 · 支持一天多段", 12)
            .apply { setTextColor(color(R.color.text_muted)) })

        weekBlock.isVisible = isWeek
        simpleBlock.isVisible = !isWeek
        host.addView(cardBlock("时间表", vertical(0).apply { addView(weekBlock); addView(simpleBlock) }))
        modeGroup.setOnCheckedChangeListener { _, checked ->
            weekBlock.isVisible = checked == weekRadio.id
            simpleBlock.isVisible = checked != weekRadio.id
            scheduleConfigAutosave()
        }

        // ---- 座位优先级（增删改立即入库，与网页端一致）----
        val chosenSeats = jsonArrayStrings(cfg.optJSONArray("seat_list")).toMutableList()
        val seatList = vertical(0)
        fun renderChosenSeats() {
            seatList.removeAllViews()
            chosenSeats.forEachIndexed { index, name ->
                val row = horizontal().apply {
                    setBackgroundResource(R.drawable.bg_native_field)
                    layoutParams = LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                    ).apply { bottomMargin = dp(6) }
                }
                val handle = text("⋮⋮", 16, true).apply {
                    setTextColor(color(R.color.text_muted)); gravity = Gravity.CENTER
                }
                row.addView(handle, LinearLayout.LayoutParams(dp(34), dp(46)))
                row.addView(text("${index + 1}. $name", 15), LinearLayout.LayoutParams(0, dp(46), 1f))
                row.addView(action("×", compact = true, danger = true) {
                    chosenSeats.removeAt(index)
                    renderChosenSeats()
                    persistSeatList(chosenSeats)
                })
                enableSeatDrag(handle, row, chosenSeats.size, index) { from, to ->
                    chosenSeats.add(to, chosenSeats.removeAt(from))
                    renderChosenSeats()
                    persistSeatList(chosenSeats)
                }
                seatList.addView(row)
            }
            if (chosenSeats.isEmpty()) {
                seatList.addView(text("还没有候选座位，抢座时无座可抢。", 13)
                    .apply { setTextColor(color(R.color.text_muted)) })
            } else {
                seatList.addView(text("按住 ⋮⋮ 上下拖动可调整优先级", 12)
                    .apply { setTextColor(color(R.color.text_muted)) })
            }
            seatList.addView(action("＋ 添加候选座位", accent = true) {
                pickSeat { name ->
                    if (chosenSeats.contains(name)) return@pickSeat toast("座位已存在")
                    chosenSeats += name
                    renderChosenSeats()
                    persistSeatList(chosenSeats)
                    toast("已添加并保存 $name")
                }
            })
        }
        renderChosenSeats()
        host.addView(cardBlock("座位优先级（按顺序尝试）", seatList))

        // ---- 功能开关 ----
        val autoReserve = SwitchMaterial(this).apply {
            text = "自动预约"; isChecked = cfg.optString("is_reserved") == "True"
            setOnCheckedChangeListener { _, _ -> scheduleConfigAutosave() }
        }
        val lateProtection = SwitchMaterial(this).apply {
            text = "迟到保护 🛡"; isChecked = cfg.optString("late_protection") == "True"
        }
        // 关闭随时生效；首次开启先弹说明，确认后才真正打开（与网页端一致，只提示一次）。
        lateProtection.setOnCheckedChangeListener { view, checked ->
            if (!checked || getPreferences(MODE_PRIVATE).getBoolean(PREF_LP_ACK, false)) {
                scheduleConfigAutosave()
                return@setOnCheckedChangeListener
            }
            AlertDialog.Builder(this).setTitle("🛡 关于迟到保护").setMessage(LATE_PROTECTION_INFO)
                .setNegativeButton("取消") { _, _ -> view.isChecked = false }
                .setPositiveButton("我已知晓，永久关闭提示") { _, _ ->
                    getPreferences(MODE_PRIVATE).edit().putBoolean(PREF_LP_ACK, true).apply()
                    scheduleConfigAutosave()
                }
                .setCancelable(false)
                .show()
        }
        val autoNap = SwitchMaterial(this).apply {
            text = "自动午休 😴"; isChecked = napConfig.optBoolean("auto_daily")
        }
        host.addView(cardBlock("功能开关", vertical(0).apply {
            addView(autoReserve)
            addView(text("每日定时自动执行。关闭后暂停所有自动预约。", 12).apply { setTextColor(color(R.color.text_muted)) })
            addView(lateProtection)
            addView(text("未到馆时自动推迟预约最多 1 小时。", 12).apply { setTextColor(color(R.color.text_muted)) })
            addView(autoNap)
            addView(text("每日到「午休开始」时刻自动续约下午时段。", 12).apply { setTextColor(color(R.color.text_muted)) })
        }))

        // ---- 凭据 ----
        val vpn = input("统一身份认证密码（网上办事大厅）", password = true).apply { setText(cfg.optString("vpn_password")) }
        host.addView(cardBlock("统一身份认证", vertical(0).apply { addView(vpn) }))

        collectConfigBody = {
            val weekMode = modeGroup.checkedRadioButtonId == weekRadio.id
            val week = JSONObject()
            dayRows.forEachIndexed { index, row ->
                val day = index + 1
                if (!row.enabled.isChecked) week.put(day.toString(), "休息")
                else week.put(day.toString(), JSONArray(
                    collectSegments(row.segments, day).ifEmpty { listOf(WEEK_DEFAULTS[index]) }
                ))
            }
            val time = JSONObject().put("week_time", week)
            if (!weekMode) {
                time.put("tomorrow", JSONArray(collectSegments(simpleList, null).ifEmpty { listOf("08:00-22:00") }))
            }
            JSONObject()
                .put("mode", if (weekMode) "week_time" else "tomorrow")
                .put("time", time)
                .put("seat_list", JSONArray(chosenSeats))
                .put("is_reserved", if (autoReserve.isChecked) "True" else "False")
                .put("late_protection", if (lateProtection.isChecked) "True" else "False")
        }
        collectNapAuto = { autoNap.isChecked }

        val saveRow = horizontal()
        saveRow.addView(action("保存配置", 1f, accent = true) { saveConfiguration(vpn.text.toString()) })
        saveRow.addView(action("验证并保存", 1f) {
            if (vpn.text.isBlank()) return@action toast("请填写统一身份认证密码")
            setBusy(true)
            api.post("/api/my/accounts/${api.encoded(currentPid)}/verify", JSONObject()
                .put("vpn_password", vpn.text.toString())) { response ->
                if (response.jsonObject?.optBoolean("verified") == true) {
                    saveConfiguration(vpn.text.toString(), verified = true)
                } else { setBusy(false); toast(response.message("验证失败")) }
            }
        })
        host.addView(saveRow)
        host.addView(action("⚡ 立即预约", accent = true) { showReserveDialog() })
        binding.content.post { configEditable = true }
    }

    private fun scheduleConfigAutosave() {
        if (!configEditable || currentPid.isBlank() || currentConfig == null || collectConfigBody == null) return
        autosaveRunnable?.let { autosaveHandler.removeCallbacks(it) }
        val runnable = Runnable { autosaveRunnable = null; saveConfiguration(silent = true) }
        autosaveRunnable = runnable
        autosaveHandler.postDelayed(runnable, 700)
    }

    private fun flushConfigAutosave() {
        val pending = autosaveRunnable ?: return
        autosaveHandler.removeCallbacks(pending)
        autosaveRunnable = null
        pending.run()
    }

    /**
     * 保存配置。静默保存（自动保存）不提交密码，避免用户还没输完就把
     * vpn_password 覆盖成半截字符串并触发后端把 verified 重置为 false。
     */
    private fun saveConfiguration(vpnPassword: String? = null, verified: Boolean = false, silent: Boolean = false) {
        val body = collectConfigBody?.invoke() ?: run { if (!silent) setBusy(false); return }
        if (!silent && vpnPassword != null) body.put("vpn_password", vpnPassword)
        if (verified) body.put("verified", true)
        if (!silent) setBusy(true)
        api.post("/api/my/accounts/${api.encoded(currentPid)}", body) { response ->
            if (!response.ok) {
                if (!silent) setBusy(false)
                return@post toast((if (silent) "自动保存失败：" else "") + response.message("保存失败"))
            }
            currentConfig?.let { cfg -> body.keys().forEach { key -> cfg.put(key, body.get(key)) } }
            accountSummary(currentPid)?.let { summary -> body.keys().forEach { key -> summary.put(key, body.get(key)) } }
            fun saved() {
                setBusy(false)
                toast(if (verified) "验证并保存成功" else "配置已保存")
                binding.navigation.selectedItemId = PAGE_HOME
                loadInitialData()
            }
            val napAuto = collectNapAuto?.invoke()
            if (!silent && napAuto != null && napAuto != napConfig.optBoolean("auto_daily")) {
                val updated = copyNapConfig().put("auto_daily", napAuto)
                api.post("/api/my/accounts/${api.encoded(currentPid)}/nap_config", updated) { napResponse ->
                    if (napResponse.ok) napConfig = updated
                    saved()
                }
            } else if (!silent) saved()
        }
    }

    /**
     * 手柄拖拽重排座位优先级（对应网页端的 chip 拖拽）。只在手柄上接管触摸事件，
     * 行内的 × 按钮才能照常点击；拖拽期间要求父级 ScrollView 别抢走垂直滑动。
     */
    @SuppressLint("ClickableViewAccessibility")
    private fun enableSeatDrag(
        handle: View,
        row: View,
        total: Int,
        index: Int,
        onReorder: (Int, Int) -> Unit,
    ) {
        var downY = 0f
        handle.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downY = event.rawY
                    row.alpha = 0.85f
                    row.translationZ = dp(6).toFloat()
                    view.parent?.requestDisallowInterceptTouchEvent(true)
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    row.translationY = event.rawY - downY
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    val step = row.height + dp(6)
                    val shift = if (step > 0) Math.round(row.translationY / step) else 0
                    row.translationY = 0f
                    row.translationZ = 0f
                    row.alpha = 1f
                    view.parent?.requestDisallowInterceptTouchEvent(false)
                    val target = (index + shift).coerceIn(0, (total - 1).coerceAtLeast(0))
                    if (event.actionMasked == MotionEvent.ACTION_UP && target != index) {
                        onReorder(index, target)
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun persistSeatList(list: List<String>) {
        val pid = currentPid
        if (pid.isBlank() || currentConfig == null) return
        val payload = JSONArray(list)
        currentConfig?.put("seat_list", payload)
        accountSummary(pid)?.put("seat_list", payload)
        api.postSerial("/api/my/accounts/${api.encoded(pid)}", JSONObject().put("seat_list", payload)) { response ->
            if (!response.ok) toast("座位保存失败：" + response.message("请稍后重试"))
        }
    }
    // endregion

    // region Settings and accounts
    private fun renderSettings() {
        val host = pageHost()
        host.addView(section("账号"))
        val accountBody = vertical(0)
        accountBody.addView(text(if (loggedIn()) displayName() else "游客", 20, true))
        accountBody.addView(text(when {
            !loggedIn() -> "游客模式 · 仅可浏览，添加学号即登录"
            auth.optString("nickname").isNotBlank() -> "已登录 · @${auth.optString("uid")} · ${accounts.length()} 个学号"
            else -> "已登录 · ${accounts.length()} 个学号"
        }, 14))
        val authRow = horizontal()
        if (loggedIn()) authRow.addView(action("编辑资料", 1f) { showProfileDialog() })
        authRow.addView(action("＋ 添加学号", 1f, accent = !loggedIn()) { showAddAccountDialog() })
        accountBody.addView(authRow)
        host.addView(cardBlock("AutoLib 身份", accountBody))

        val libraryBody = vertical(0)
        for (i in 0 until accounts.length()) {
            val item = accounts.optJSONObject(i) ?: continue
            val pid = item.optString("pid")
            val row = horizontal()
            row.addView(vertical(0).apply {
                addView(text(if (pid == currentPid) "✓ $pid" else pid, 16, pid == currentPid))
                addView(text(buildString {
                    append(if (item.optString("mode") == "week_time") "按星期" else "统一时段")
                    append(" · ").append(if (item.optString("is_reserved") == "True") "运行中" else "已暂停")
                    append(" · ").append(if (item.optBoolean("verified")) "已验证" else "未验证")
                }, 12).apply { setTextColor(color(R.color.text_muted)) })
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            if (pid != currentPid) row.addView(action("切换", compact = true) { switchAccount(pid) })
            row.addView(action("删除", compact = true, danger = true) { confirmDeleteAccount(pid) })
            libraryBody.addView(row)
        }
        if (accounts.length() == 0) libraryBody.addView(text("还没有绑定学号。", 13)
            .apply { setTextColor(color(R.color.text_muted)) })
        libraryBody.addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() })
        host.addView(cardBlock("图书馆学号", libraryBody))

        host.addView(section("提醒"))
        val notifySwitch = SwitchMaterial(this).apply {
            text = "抢座结果通知"
            isChecked = ReservationAlarm.isEnabled(this@MainActivity) && ReservationSync.hasPermission(this@MainActivity)
        }
        notifySwitch.setOnCheckedChangeListener { view, checked ->
            if (!checked) {
                ReservationAlarm.setEnabled(this, false)
                return@setOnCheckedChangeListener
            }
            if (!ReservationSync.hasPermission(this)) {
                view.isChecked = false
                requestNotificationPermission()
                return@setOnCheckedChangeListener
            }
            ReservationSync.ensureChannel(this)
            ReservationAlarm.setEnabled(this, true)
            toast("已开启，${ReservationAlarm.describe()}")
        }
        host.addView(cardBlock("通知", vertical(0).apply {
            addView(notifySwitch)
            addView(text("后端固定 7:00 抢座，App ${ReservationAlarm.describe()}结果并推送。", 12)
                .apply { setTextColor(color(R.color.text_muted)) })
        }))

        host.addView(section("桌面"))
        host.addView(cardBlock("桌面小组件", vertical(0).apply {
            addView(text("三种规格：2×2 专注今天、4×2 今天加明日、4×4 完整掌控一周。", 12)
                .apply { setTextColor(color(R.color.text_muted)) })
            addView(action("添加到桌面", accent = true) { requestPinWidget() })
        }))

        host.addView(section("外观"))
        host.addView(cardBlock("主题", horizontal().apply {
            addView(vertical(0).apply {
                addView(text(THEME_LABELS[storedTheme()] ?: "跟随系统", 16, true))
                addView(text("当前使用的配色方案。", 12).apply { setTextColor(color(R.color.text_muted)) })
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(themeSwitcher())
        }))

        host.addView(section("通知"))
        host.addView(cardBlock("预约结果通知", vertical(0).apply {
            addView(text("每天抢座的成败、自动午休的结果都会发到邮箱。", 12)
                .apply { setTextColor(color(R.color.text_muted)) })
            addView(notifyRow(
                "邮箱",
                currentConfig?.optString("notify_email").orEmpty(),
            ) { showEmailDialog() })
        }))

        host.addView(section("午休"))
        host.addView(cardBlock("午休配置", vertical(0).apply {
            addView(text(buildString {
                append("午休 ").append(napConfig.optString("trigger_time").ifBlank { "12:00" })
                append("–").append(napConfig.optString("start_time").ifBlank { "14:00" })
                append("，").append(napConfig.optString("seat").ifBlank { "同座位" })
                if (napConfig.optBoolean("auto_daily")) append("，每日自动")
            }, 14))
            addView(action("编辑", accent = true) { showNapSettingsDialog() })
        }))

        host.addView(section("关于"))
        host.addView(cardBlock("版本", vertical(0).apply {
            addView(text("AutoLib ${BuildConfig.VERSION_NAME}", 16, true))
            // 默认每天自动检查，属于预期行为不必说明；只有被用户关掉时才需要提示，
            // 否则他既看不出状态、也找不到恢复入口。
            if (!UpdateChecker.autoCheckEnabled(this@MainActivity)) {
                addView(text("自动检查已关闭", 12)
                    .apply { setTextColor(color(R.color.text_muted)) })
            }
            addView(horizontal().apply {
                addView(action("检查更新", 1f, accent = true) { checkUpdateManually() })
                if (!UpdateChecker.autoCheckEnabled(this@MainActivity)) {
                    addView(action("恢复自动检查", 1f) {
                        UpdateChecker.setAutoCheckEnabled(this@MainActivity, true)
                        toast("已恢复每天自动检查")
                        renderCurrentPage()
                    })
                }
            })
        }))
        host.addView(cardBlock("功能说明", vertical(0).apply {
            addView(action("迟到保护是什么？") { showLateProtectionInfo() })
            addView(action("午休是什么？") { showNapInfo() })
        }))

        if (loggedIn()) {
            host.addView(section("其他"))
            host.addView(action("退出登录", danger = true) { logout() })
        }
    }

    /** 「学习记录」整块：周/累计统计 + 热力图 + 最近几次。异步填充，先占位。 */
    private fun visitStatsCard(host: LinearLayout) {
        val stats = card().also {
            it.addView(vertical(dp(18)).apply { addView(text("加载中…", 14)) })
            host.addView(it)
        }
        api.get("/api/my/visit_stats") { response ->
            val data = response.jsonObject ?: JSONObject()
            val content = vertical(0)
            if (data.optInt("total_visits") == 0) {
                content.addView(text("还没有学习记录", 16, true))
                content.addView(text("系统检测到签到后自动记录。", 13).apply { setTextColor(color(R.color.text_muted)) })
            } else {
                content.addView(text("本周 ${data.optInt("this_week_visits")} 次 · ${formatMinutes(data.optInt("this_week_minutes"))}", 19, true))
                content.addView(text("累计 ${data.optInt("total_visits")} 次 · ${formatMinutes(data.optInt("total_minutes"))}", 15))
                val recent = data.optJSONArray("recent") ?: JSONArray()
                // 老版本后端没有 daily 字段，用 recent 兜底才不至于画出一张空图。
                // recent 只有最近 10 条，热力图会不全——服务端更新后就走 daily。
                val daily = data.optJSONArray("daily")?.takeIf { it.length() > 0 }
                    ?: dailyFromRecent(recent)
                content.addView(heatmapBlock(daily, data.optInt("heatmap_days", 371)))
            }
            replaceCard(stats, content)
        }
    }

    /** 把 recent 列表按日期聚合成 daily 的形状，供缺少 daily 字段的旧后端兜底。 */
    private fun dailyFromRecent(recent: JSONArray): JSONArray {
        val byDate = LinkedHashMap<String, Pair<Int, Int>>()
        for (i in 0 until recent.length()) {
            val item = recent.optJSONObject(i) ?: continue
            val date = item.optString("date").takeIf { it.isNotBlank() } ?: continue
            val (visits, minutes) = byDate[date] ?: (0 to 0)
            byDate[date] = visits + 1 to minutes + item.optInt("duration_minutes")
        }
        val out = JSONArray()
        byDate.forEach { (date, agg) ->
            out.put(JSONObject()
                .put("date", date)
                .put("visits", agg.first)
                .put("minutes", agg.second))
        }
        return out
    }

    /**
     * 热力图 + 图例。图表本身要横向滚动，图例固定在下方不跟着滚。
     *
     * 只画当前学期（下半年 7–12 月 / 上半年 1–6 月），不是"最近一年"：
     * 铺满整年大多是空格，按学期看也更贴近实际的作息周期。
     */
    private fun heatmapBlock(daily: JSONArray, days: Int): View {
        val block = vertical(0)
        val heatmap = HeatmapView(this)
        val byDate = HashMap<String, Pair<Int, Int>>()
        for (i in 0 until daily.length()) {
            val item = daily.optJSONObject(i) ?: continue
            byDate[item.optString("date")] = item.optInt("visits") to item.optInt("minutes")
        }

        val format = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        val today = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, 0); set(Calendar.MINUTE, 0)
            set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
        }
        val todayKey = format.format(today.time)
        val firstHalf = today.get(Calendar.MONTH) < Calendar.JULY
        val cursor = semesterStart(today, firstHalf)
        val semesterLabel = "${cursor.get(Calendar.YEAR)}年${if (firstHalf) "上" else "下"}半年"
        // 对齐到周一，保证每一列都是完整的一周
        cursor.add(Calendar.DAY_OF_YEAR, -(isoOf(cursor) - 1))

        val cells = mutableListOf<HeatmapView.Cell>()
        val months = mutableListOf<String?>()
        var lastMonth = -1
        while (!cursor.after(today)) {
            if (cells.size % 7 == 0) {
                val month = cursor.get(Calendar.MONTH)
                months += if (month != lastMonth) "${month + 1}月" else null
                lastMonth = month
            }
            val key = format.format(cursor.time)
            val hit = byDate[key]
            cells += HeatmapView.Cell(key, hit?.second ?: 0, hit?.first ?: 0, key == todayKey)
            cursor.add(Calendar.DAY_OF_YEAR, 1)
        }
        while (cells.size % 7 != 0) {
            cells += HeatmapView.Cell("", 0, 0, isToday = false, filler = true)
        }
        heatmap.cells = cells
        heatmap.monthLabels = months

        val scroll = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            addView(heatmap)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(10) }
        }
        block.addView(scroll)
        // 最近的日期在最右侧，打开时直接滚过去
        scroll.post { scroll.fullScroll(View.FOCUS_RIGHT) }

        val legend = horizontal().apply { setPadding(0, dp(8), 0, dp(4)) }
        legend.addView(text("少", 11).apply { setTextColor(color(R.color.text_muted)) })
        listOf(0, 60, 180, 300, 480).forEach { minutes ->
            legend.addView(View(this).apply {
                setBackgroundColor(color(HeatmapView.heatColor(minutes)))
                layoutParams = LinearLayout.LayoutParams(dp(12), dp(12)).apply { marginStart = dp(3) }
            })
        }
        legend.addView(text("多", 11).apply {
            setTextColor(color(R.color.text_muted)); setPadding(dp(5), 0, 0, 0)
        })
        legend.addView(text(semesterLabel, 11).apply {
            setTextColor(color(R.color.text_muted)); gravity = Gravity.END
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        block.addView(legend)
        return block
    }

    /** 学期起点：上半年从 1 月 1 日算，下半年从 7 月 1 日算。 */
    private fun semesterStart(today: Calendar, firstHalf: Boolean): Calendar =
        (today.clone() as Calendar).apply {
            set(Calendar.MONTH, if (firstHalf) Calendar.JANUARY else Calendar.JULY)
            set(Calendar.DAY_OF_MONTH, 1)
        }

    private fun showProfileDialog() {
        val body = vertical()
        val nickname = input("昵称（可选）").apply { setText(auth.optString("nickname")) }
        body.addView(text("昵称会替代学号 ${auth.optString("uid")} 显示。密码即统一身份认证密码，需在学校的系统里修改。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        body.addView(nickname)
        AlertDialog.Builder(this).setTitle("账号资料").setView(body).setNegativeButton("取消", null).setPositiveButton("保存") { _, _ ->
            api.post("/api/auth/profile", JSONObject().put("nickname", nickname.text.toString().trim())) { response ->
                toast(response.message("已更新"))
                if (response.ok) loadInitialData()
            }
        }.show()
    }

    /** 设置页里「渠道名 + 当前值 + 编辑」的一行。 */
    private fun notifyRow(label: String, value: String, onEdit: () -> Unit) = horizontal().apply {
        addView(vertical(0).apply {
            addView(text(label, 15, true))
            addView(text(value.ifBlank { "未设置" }, 13).apply {
                setTextColor(color(if (value.isBlank()) R.color.text_muted else R.color.text_secondary))
            })
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(action("编辑", compact = true) { onEdit() })
    }

    private fun showEmailDialog() {
        val cfg = currentConfig ?: return toast("请先添加学号")
        val body = vertical()
        body.addView(text("预约结果通过邮件推送。留空即不发送。", 13).apply { setTextColor(color(R.color.text_secondary)) })
        val email = input("邮箱地址").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText(cfg.optString("notify_email"))
        }
        body.addView(email)
        AlertDialog.Builder(this).setTitle("邮箱通知").setView(body).setNegativeButton("取消", null).setPositiveButton("保存") { _, _ ->
            val value = email.text.toString().trim()
            if (value.isNotBlank() && !value.contains('@')) return@setPositiveButton toast("邮箱地址格式不正确")
            saveNotifyField(cfg, "notify_email", value, "邮箱已保存")
        }.show()
    }

    private fun saveNotifyField(cfg: JSONObject, field: String, value: String, success: String) {
        api.post("/api/my/accounts/${api.encoded(currentPid)}", JSONObject().put(field, value)) { response ->
            toast(response.message(success))
            if (response.ok) {
                cfg.put(field, value)
                accountSummary(currentPid)?.put(field, value)
                renderCurrentPage()
            }
        }
    }

    private fun showNapSettingsDialog() {
        if (currentPid.isBlank()) return toast("请先添加学号")
        val body = vertical()
        body.addView(text("默认 12:00 出门，14:00 回来。下午时段自动续约到原预约结束。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        val trigger = spinner(timeOptions("08:00", "22:00"))
            .apply { selectValue(napConfig.optString("trigger_time").ifBlank { "12:00" }) }
        val back = spinner(timeOptions("08:00", dayMax(isoToday())))
            .apply { selectValue(napConfig.optString("start_time").ifBlank { "14:00" }) }
        body.addView(labeled("午休开始", trigger)); body.addView(labeled("午休结束", back))
        val savedSeat = napConfig.optString("seat")
        val seatMode = spinner(listOf("当前预约的座位（自动）", "固定自定义座位"))
            .apply { setSelection(if (savedSeat.isNotBlank()) 1 else 0) }
        body.addView(labeled("默认座位", seatMode))
        val zone = spinner(sortedZones())
        val seatPick = spinner(emptyList())
        bindZoneToSeats(zone, seatPick)
        val customBlock = vertical(0).apply {
            addView(labeled("楼层 / 区域", zone)); addView(labeled("座位号", seatPick))
            isVisible = savedSeat.isNotBlank()
        }
        if (savedSeat.isNotBlank()) selectSeat(zone, seatPick, savedSeat)
        seatMode.onItemSelectedListener = simpleSelection { customBlock.isVisible = seatMode.selectedItemPosition == 1 }
        body.addView(customBlock)
        val autoDaily = SwitchMaterial(this).apply {
            text = "每日自动午休"; isChecked = napConfig.optBoolean("auto_daily")
        }
        body.addView(autoDaily)
        body.addView(text("每天到「午休开始」时刻自动执行。", 12).apply { setTextColor(color(R.color.text_muted)) })

        val dialog = AlertDialog.Builder(this).setTitle("😴 午休配置").setView(scrolled(body))
            .setNegativeButton("取消", null).setPositiveButton("保存", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val triggerValue = trigger.selectedItem.toString()
                val backValue = back.selectedItem.toString()
                if (triggerValue >= backValue) return@setOnClickListener toast("午休结束必须晚于午休开始")
                var seatValue = ""
                if (seatMode.selectedItemPosition == 1) {
                    seatValue = seatPick.selectedItem?.toString().orEmpty()
                    if (seatValue.isBlank()) return@setOnClickListener toast("请先选择自定义座位")
                }
                saveNapConfig(JSONObject()
                    .put("start_time", backValue).put("end_time", "").put("seat", seatValue)
                    .put("auto_daily", autoDaily.isChecked).put("trigger_time", triggerValue)) {
                    dialog.dismiss()
                    renderCurrentPage()
                }
            }
        }
        dialog.show()
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_NOTIFICATIONS) return
        val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        if (granted) {
            ReservationSync.ensureChannel(this)
            ReservationAlarm.setEnabled(this, true)
            toast("已开启，${ReservationAlarm.describe()}")
        } else {
            toast("未授予通知权限，可在系统设置里手动打开")
        }
        renderCurrentPage()
    }

    /**
     * 请求把小组件钉到桌面。Android 8.0 起支持，省得用户自己长按桌面找；
     * 少数第三方桌面不支持，这时提示手动添加。
     */
    /**
     * 升级提示。三个出口对应三种意图：现在就装 / 这版先算了 / 以后别自动弹。
     * 「跳过此版本」只记住当前 versionCode，更新的版本仍会提示；
     * 「永不提示」只关自动检查，设置页里手动检查照常可用。
     */
    private fun showUpdateDialog(release: UpdateChecker.Release) {
        if (isFinishing || isDestroyed) return
        val body = vertical()
        body.addView(text("发现新版本 ${release.versionName}", 17, true))
        body.addView(text("当前版本 ${BuildConfig.VERSION_NAME}", 13)
            .apply { setTextColor(color(R.color.text_muted)) })
        if (release.notes.isNotBlank()) {
            body.addView(text(release.notes, 14).apply { setPadding(0, dp(10), 0, 0) })
        }
        AlertDialog.Builder(this)
            .setTitle("检查到更新")
            .setView(scrolled(body))
            .setPositiveButton("立即更新") { _, _ -> openDownload(release.downloadUrl) }
            .setNegativeButton("跳过此版本") { _, _ ->
                UpdateChecker.skip(this, release.versionCode)
                toast("已跳过 ${release.versionName}，有更新版本时会再提醒")
            }
            .setNeutralButton("永不提示") { _, _ ->
                UpdateChecker.setAutoCheckEnabled(this, false)
                toast("已关闭自动检查，可在设置页手动检查")
                if (currentPage == PAGE_SETTINGS) renderCurrentPage()
            }
            .show()
    }

    private fun openDownload(url: String) {
        val intent = android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url))
        // 没有浏览器时直接 startActivity 会抛 ActivityNotFoundException
        if (intent.resolveActivity(packageManager) != null) startActivity(intent)
        else toast("没有可用的浏览器，请手动访问：$url")
    }

    private fun checkUpdateManually() {
        setBusy(true)
        UpdateChecker.checkManually(this, api) { release, error ->
            setBusy(false)
            when {
                error != null -> toast(error)
                release != null -> showUpdateDialog(release)
                else -> toast("已是最新版本 ${BuildConfig.VERSION_NAME}")
            }
        }
    }

    private fun requestPinWidget() {
        val manager = getSystemService(AppWidgetManager::class.java)
        if (manager == null || !manager.isRequestPinAppWidgetSupported) {
            return toast("当前桌面不支持一键添加，请长按桌面手动添加 AutoLib 小组件")
        }
        val options = listOf(
            "2×2 · 专注今天" to SeatWidgetSmallProvider::class.java,
            "4×2 · 今天 + 明日" to SeatWidgetProvider::class.java,
            "4×4 · 完整掌控一周" to SeatWidgetLargeProvider::class.java,
        )
        AlertDialog.Builder(this)
            .setTitle("选择小组件规格")
            .setItems(options.map { it.first }.toTypedArray()) { _, index ->
                manager.requestPinAppWidget(ComponentName(this, options[index].second), null, null)
                SeatWidgets.refresh(this)
            }
            .setNegativeButton("取消", null)
            .show()
    }

    // ---- 主题：跟随系统 / 亮色 / 暗色，三档循环，与网页端一致 ----
    private fun storedTheme() = getPreferences(MODE_PRIVATE).getString(PREF_THEME, THEME_SYSTEM) ?: THEME_SYSTEM

    private fun nightModeOf(theme: String) = when (theme) {
        THEME_LIGHT -> AppCompatDelegate.MODE_NIGHT_NO
        THEME_DARK -> AppCompatDelegate.MODE_NIGHT_YES
        else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
    }

    /**
     * 三档并列的分段控件：跟随系统 / 亮色 / 暗色，选中的那格用卡片色抬起来。
     * 比原来的「切换主题」按钮少一次盲猜——三个选项和当前状态一眼可见。
     */
    private fun themeSwitcher(): View {
        val current = storedTheme()
        val options = listOf(
            THEME_SYSTEM to R.drawable.ic_theme_system,
            THEME_LIGHT to R.drawable.ic_theme_light,
            THEME_DARK to R.drawable.ic_theme_dark,
        )
        return horizontal().apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.RECTANGLE
                cornerRadius = dp(12).toFloat()
                setColor(color(R.color.surface_alt))
                setStroke(dp(1), color(R.color.stroke_muted))
            }
            setPadding(dp(3), dp(3), dp(3), dp(3))
            options.forEach { (theme, icon) ->
                val selected = theme == current
                addView(androidx.appcompat.widget.AppCompatImageButton(this@MainActivity).apply {
                    setImageResource(icon)
                    imageTintList = ColorStateList.valueOf(
                        color(if (selected) R.color.text_primary else R.color.text_muted)
                    )
                    background = if (selected) GradientDrawable().apply {
                        shape = GradientDrawable.RECTANGLE
                        cornerRadius = dp(10).toFloat()
                        setColor(color(R.color.surface))
                        setStroke(dp(1), color(R.color.stroke))
                    } else null
                    scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
                    setPadding(dp(7), dp(7), dp(7), dp(7))
                    contentDescription = THEME_LABELS[theme]
                    layoutParams = LinearLayout.LayoutParams(dp(38), dp(34)).apply {
                        marginStart = if (theme == THEME_SYSTEM) 0 else dp(2)
                    }
                    setOnClickListener { applyTheme(theme) }
                })
            }
        }
    }

    private fun applyTheme(theme: String) {
        if (theme == storedTheme()) return
        getPreferences(MODE_PRIVATE).edit().putString(PREF_THEME, theme).apply()
        // setDefaultNightMode 会自行重建 Activity，重建后 onCreate 再读一次即可。
        AppCompatDelegate.setDefaultNightMode(nightModeOf(theme))
        renderCurrentPage()
    }

    private fun showLateProtectionInfo() {
        AlertDialog.Builder(this).setTitle("🛡 关于迟到保护").setMessage(LATE_PROTECTION_INFO)
            .setPositiveButton("我知道了", null).show()
    }

    private fun showNapInfo() {
        AlertDialog.Builder(this).setTitle("😴 一键午休").setMessage(NAP_INFO)
            .setPositiveButton("好的", null).show()
    }

    private fun logout() {
        AlertDialog.Builder(this).setTitle("退出登录？")
            .setMessage("配置仍保存在云端。重新用学号和统一身份认证密码验证一次即可找回。")
            .setNegativeButton("取消", null).setPositiveButton("退出") { _, _ ->
                flushConfigAutosave()
                api.post("/api/auth/logout") {
                    auth = JSONObject()
                    accounts = JSONArray()
                    currentPid = ""
                    currentConfig = null
                    napConfig = defaultNapConfig()
                    todayReservation = null
                    tomorrowReservation = null
                    getPreferences(MODE_PRIVATE).edit().remove("current_pid").apply()
                    toast("已退出")
                    loadInitialData()
                }
            }.show()
    }

    private fun showAccountChooser() {
        if (accounts.length() == 0) return showAddAccountDialog()
        val options = (0 until accounts.length()).map { accounts.optJSONObject(it)?.optString("pid").orEmpty() } + "＋ 添加学号"
        AlertDialog.Builder(this).setTitle("选择图书馆学号").setItems(options.toTypedArray()) { _, index ->
            if (index == options.lastIndex) showAddAccountDialog() else switchAccount(options[index])
        }.show()
    }

    private fun switchAccount(pid: String) {
        flushConfigAutosave()
        currentPid = pid
        setBusy(true)
        toast("已切换到 $pid")
        loadAccountDetail(pid)
    }

    /**
     * 添加学号即登录：验证通过后后端会把当前会话提升为该学号（`/verify` 返回
     * logged_in），因此客户端不需要单独的登录入口。
     */
    private fun showAddAccountDialog() {
        val body = vertical()
        body.addView(text("填写学号和统一身份认证（网上办事大厅）密码，验证通过后即完成登录并绑定。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        body.addView(text("🔒 学号和密码会加密保存在服务器上，仅用于每天自动预约时登录学校系统，管理员也无法看到明文。换手机后重新验证一次即可找回全部配置。", 12)
            .apply { setTextColor(color(R.color.text_muted)) })
        val pid = input("学号"); val vpn = input("统一身份认证密码", true)
        body.addView(pid); body.addView(vpn)
        val dialog = AlertDialog.Builder(this).setTitle("添加学号").setView(scrolled(body)).setNegativeButton("取消", null)
            .setPositiveButton("验证并保存", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val p = pid.text.toString().trim()
                if (p.isBlank() || vpn.text.isBlank()) return@setOnClickListener toast("请填写学号和统一身份认证密码")
                val positive = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                positive.isEnabled = false
                setBusy(true)
                fun fail(message: String) { setBusy(false); positive.isEnabled = true; toast(message) }
                api.post("/api/my/accounts/${api.encoded(p)}/verify", JSONObject()
                    .put("vpn_password", vpn.text.toString())) verified@ { verify ->
                    if (verify.jsonObject?.optBoolean("verified") != true) {
                        // 学校服务异常时后端可能凭本地缓存放行：仍进入登录态，只是未验证。
                        if (verify.jsonObject?.optBoolean("offline") == true) {
                            currentPid = p
                            getPreferences(MODE_PRIVATE).edit().putString("current_pid", p).apply()
                            dialog.dismiss()
                            toast(verify.message("已使用本地缓存登录"))
                            loadInitialData()
                        } else {
                            fail(verify.message("验证失败"))
                        }
                        return@verified
                    }
                    currentPid = p
                    getPreferences(MODE_PRIVATE).edit().putString("current_pid", p).apply()
                    // 云端已有该学号的配置时直接恢复，绝不用默认值覆盖用户已保存的规则。
                    api.get("/api/my/accounts") { listResponse ->
                        accounts = listResponse.jsonArray ?: JSONArray()
                        if (accountExists(p)) {
                            dialog.dismiss()
                            toast("已恢复学号 $p 的云端配置")
                            loadInitialData()
                            return@get
                        }
                        api.post("/api/my/accounts/${api.encoded(p)}", JSONObject()
                            .put("vpn_password", vpn.text.toString())
                            .put("seat_list", JSONArray())
                            .put("mode", "week_time")
                            .put("time", defaultWeekTime())
                            .put("verified", true)) { saved ->
                            if (!saved.ok) {
                                fail(saved.message("保存失败"))
                            } else {
                                dialog.dismiss()
                                toast("已添加学号 $p")
                                loadInitialData()
                            }
                        }
                    }
                }
            }
        }
        dialog.show()
    }

    private fun confirmDeleteAccount(pid: String) {
        AlertDialog.Builder(this).setTitle("删除学号 $pid？").setMessage("将删除该学号的预约配置，此操作无法撤销。")
            .setNegativeButton("取消", null).setPositiveButton("删除") { _, _ ->
                api.delete("/api/my/accounts/${api.encoded(pid)}") { response ->
                    toast(response.message("已删除"))
                    if (response.ok) {
                        if (currentPid == pid) {
                            currentPid = ""
                            currentConfig = null
                            getPreferences(MODE_PRIVATE).edit().remove("current_pid").apply()
                        }
                        loadAccounts()
                    }
                }
            }.show()
    }
    // endregion

    // region UI helpers
    private fun pageHost(): LinearLayout {
        collectConfigBody = null
        collectNapAuto = null
        configEditable = false
        binding.content.removeAllViews()
        val scroll = ScrollView(this).apply { isFillViewport = true }
        val host = vertical(0).apply { setPadding(dp(18), 0, dp(18), dp(30)) }
        scroll.addView(host); binding.content.addView(scroll)
        return host
    }

    private fun scrolled(child: View) = ScrollView(this).apply {
        addView(child)
        setPadding(dp(8), 0, dp(8), 0)
    }

    private fun vertical(padding: Int = dp(12)) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; setPadding(padding, padding, padding, padding)
    }
    private fun horizontal() = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
    private fun text(value: String, size: Int, bold: Boolean = false) = TextView(this).apply {
        text = value; textSize = size.toFloat(); setTextColor(color(R.color.text_primary)); setPadding(0, dp(4), 0, dp(4))
        typeface = Typeface.create(if (bold) "sans-serif-rounded" else "sans-serif", if (bold) Typeface.BOLD else Typeface.NORMAL)
        setLineSpacing(0f, 1.12f)
    }
    private fun section(value: String) = text(value, 13, true).apply {
        setTextColor(color(R.color.text_muted)); setPadding(dp(4), dp(20), 0, dp(8)); letterSpacing = 0.13f
    }

    /** 小节标题 + 右侧配件，配件跟标题同一基线，不额外占一行。 */
    private fun sectionRow(value: String, trailing: View) = horizontal().apply {
        addView(section(value), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        addView(trailing)
    }

    /**
     * 主页刷新。用小节标题的静音色描边圆钮，和旁边的分组标题是同一层视觉，
     * 不会喧宾夺主；点下去转一圈作为"已受理"的反馈。
     */
    private fun refreshButton() = androidx.appcompat.widget.AppCompatImageButton(this).apply {
        setImageResource(R.drawable.ic_refresh_native)
        imageTintList = ColorStateList.valueOf(color(R.color.text_muted))
        background = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(999).toFloat()
            setColor(color(R.color.surface))
            setStroke(dp(1), color(R.color.stroke_muted))
        }
        scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
        setPadding(dp(8), dp(8), dp(8), dp(8))
        contentDescription = "刷新预约"
        layoutParams = LinearLayout.LayoutParams(dp(34), dp(34)).apply {
            topMargin = dp(6)
            bottomMargin = dp(6)
        }
        // 紧跟其后的卡片有 3dp elevation，Z 更高会压住按钮下缘，这里抬到它上面
        translationZ = dp(6).toFloat()
        setOnClickListener {
            animate().rotationBy(360f).setDuration(520L).start()
            renderHome(refresh = true)
        }
    }
    private fun card(): MaterialCardView = MaterialCardView(this).apply {
        radius = dp(15).toFloat(); cardElevation = dp(3).toFloat(); strokeWidth = dp(2); strokeColor = color(R.color.stroke)
        setCardBackgroundColor(color(R.color.surface)); useCompatPadding = true
        isClickable = false
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(10) }
    }
    private fun cardBlock(title: String, child: View) = card().apply {
        addView(vertical(dp(18)).apply {
            addView(text(title, 17, true).apply { setPadding(0, 0, 0, dp(9)) })
            addView(child)
        })
    }
    private fun noticeCard(title: String, body: String, accent: Int) = card().apply {
        strokeColor = color(accent)
        addView(vertical(dp(15)).apply {
            addView(text(title, 15, true).apply { setTextColor(color(accent)) })
            addView(text(body, 13).apply { setTextColor(color(R.color.text_secondary)) })
        })
    }
    private fun action(
        label: String,
        weight: Float? = null,
        danger: Boolean = false,
        accent: Boolean = false,
        compact: Boolean = false,
        onClick: () -> Unit,
    ) = MaterialButton(this).apply {
        text = label; isAllCaps = false; textSize = if (compact) 13f else 14f; setOnClickListener { onClick() }
        typeface = Typeface.create("sans-serif-rounded", Typeface.BOLD)
        cornerRadius = dp(10); strokeWidth = dp(2)
        backgroundTintList = ColorStateList.valueOf(color(if (accent) R.color.primary else R.color.surface))
        strokeColor = ColorStateList.valueOf(color(if (danger) R.color.danger else if (accent) R.color.primary else R.color.stroke))
        setTextColor(color(if (danger) R.color.danger else if (accent) R.color.on_primary else R.color.text_primary))
        if (compact) {
            minWidth = 0; minimumWidth = 0
            insetTop = 0; insetBottom = 0
            setPadding(dp(10), 0, dp(10), 0)
        }
        val height = if (compact) dp(40) else dp(48)
        val params = if (weight == null) LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, height)
        else LinearLayout.LayoutParams(0, height, weight)
        params.setMargins(dp(3), dp(6), dp(3), dp(4)); layoutParams = params
    }
    private fun input(hint: String, password: Boolean = false) = EditText(this).apply {
        this.hint = hint; setSingleLine(true); setPadding(dp(13), dp(8), dp(13), dp(8))
        setTextColor(color(R.color.text_primary)); setHintTextColor(color(R.color.text_muted))
        setBackgroundResource(R.drawable.bg_native_field); textSize = 14f
        typeface = Typeface.create("sans-serif-monospace", Typeface.NORMAL)
        inputType = if (password) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_CLASS_TEXT
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)).apply { bottomMargin = dp(8) }
    }
    private fun spinner(values: List<String>) = Spinner(this).apply {
        adapter = arrayAdapter(values); setBackgroundResource(R.drawable.bg_native_field); setPadding(dp(5), 0, dp(5), 0)
    }
    private fun arrayAdapter(values: List<String>) = ArrayAdapter(this, android.R.layout.simple_spinner_item, values).apply {
        setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
    }
    private fun labeled(label: String, child: View) = vertical(0).apply {
        addView(text(label.uppercase(Locale.CHINA), 11, true).apply { setTextColor(color(R.color.text_muted)); letterSpacing = 0.08f })
        addView(child, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)))
    }
    private fun replaceCard(card: MaterialCardView, child: View) { card.removeAllViews(); card.addView(vertical(dp(18)).apply { addView(child) }) }
    private fun showReservationLoading(card: MaterialCardView, message: String) = replaceCard(card, text(message, 14))
    private fun setBusy(value: Boolean) { binding.progressBar.isVisible = value }
    private fun toast(message: String) { Toast.makeText(this, message, Toast.LENGTH_LONG).show() }
    private fun color(id: Int) = ContextCompat.getColor(this, id)
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private fun styleHeroCard(card: MaterialCardView, tomorrow: Boolean) {
        card.strokeColor = color(if (tomorrow) R.color.tomorrow else R.color.primary)
        card.setCardBackgroundColor(color(if (tomorrow) R.color.tomorrow_soft else R.color.accent_soft))
        card.cardElevation = dp(4).toFloat()
    }

    private fun heroHeader(tomorrow: Boolean): View = horizontal().apply {
        addView(text(if (tomorrow) "明日座位" else "今日座位", 11, true).apply {
            setTextColor(color(R.color.text_muted)); letterSpacing = 0.1f
        }, LinearLayout.LayoutParams(0, dp(36), 1f))
        addView(statusPill(if (tomorrow) "TOMORROW" else "TODAY",
            if (tomorrow) R.color.tomorrow else R.color.primary,
            if (tomorrow) R.color.tomorrow_soft else R.color.accent_soft))
    }

    private fun statusPill(label: String, foreground: Int, background: Int) = TextView(this).apply {
        text = label; textSize = 11f; setTextColor(color(foreground)); gravity = Gravity.CENTER
        typeface = Typeface.create("sans-serif-monospace", Typeface.BOLD); letterSpacing = 0.06f
        setPadding(dp(10), dp(4), dp(10), dp(4))
        this.background = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE; cornerRadius = dp(999).toFloat()
            setColor(color(background)); setStroke(dp(1), color(foreground))
        }
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(30)).apply { marginEnd = dp(6) }
    }

    private fun pillRow(vararg pills: View?) = horizontal().apply {
        setPadding(0, dp(6), 0, dp(2))
        pills.filterNotNull().forEach { addView(it) }
    }

    private fun pickSeat(selected: (String) -> Unit) {
        if (seats.length() == 0) return toast("没有可用座位数据")
        val body = vertical(); val zone = spinner(sortedZones()); val seat = spinner(emptyList())
        bindZoneToSeats(zone, seat)
        body.addView(labeled("楼层 / 区域", zone)); body.addView(labeled("座位号", seat))
        AlertDialog.Builder(this).setTitle("添加座位").setView(body).setNegativeButton("取消", null).setPositiveButton("添加") { _, _ ->
            val name = seat.selectedItem?.toString().orEmpty()
            if (name.isBlank()) toast("请先选择") else selected(name)
        }.show()
    }

    /**
     * 区域下拉联动座位下拉。保留已选座位：Spinner 的首次 onItemSelected 是在
     * 布局后异步触发的，若无条件重建 adapter 会把预选好的座位冲掉。
     */
    private fun bindZoneToSeats(zone: Spinner, seat: Spinner) {
        zone.onItemSelectedListener = simpleSelection {
            val previous = seat.selectedItem?.toString().orEmpty()
            val values = jsonArrayStrings(seats.optJSONArray(zone.selectedItem?.toString()))
            seat.adapter = arrayAdapter(values)
            val index = values.indexOf(previous)
            if (index >= 0) seat.setSelection(index)
        }
    }

    /** 按座位名反查它所在的区域，并把两个下拉都定位过去。 */
    private fun selectSeat(zone: Spinner, seat: Spinner, name: String) {
        val owner = jsonKeys(seats).firstOrNull { jsonArrayStrings(seats.optJSONArray(it)).contains(name) }
        if (owner == null) return toast("未找到「$name」所在区域")
        zone.selectValue(owner)
        seat.adapter = arrayAdapter(jsonArrayStrings(seats.optJSONArray(owner)))
        seat.selectValue(name)
    }

    // ---- 时段行 ----
    private fun segmentRow(segment: String, isoDay: Int?, enabled: Boolean, onRemove: (View) -> Unit): View {
        val (s, e) = clampRange(segment.substringBefore('-'), segment.substringAfter('-'), isoDay)
        val max = dayMax(isoDay)
        val start = spinner(timeOptions("08:00", max)).apply {
            tag = TAG_START; selectValue(s); isEnabled = enabled
            onItemSelectedListener = simpleSelection { scheduleConfigAutosave() }
        }
        val end = spinner(timeOptions("08:00", max)).apply {
            tag = TAG_END; selectValue(e); isEnabled = enabled
            onItemSelectedListener = simpleSelection { scheduleConfigAutosave() }
        }
        val row = horizontal().apply { tag = TAG_SEGMENT }
        row.addView(start, LinearLayout.LayoutParams(0, dp(52), 1f))
        row.addView(text("—", 14).apply { gravity = Gravity.CENTER }, LinearLayout.LayoutParams(dp(24), dp(52)))
        row.addView(end, LinearLayout.LayoutParams(0, dp(52), 1f))
        row.addView(action("×", compact = true) { onRemove(row) }.apply { isEnabled = enabled })
        return row
    }

    private fun removeSegment(list: LinearLayout, row: View, warning: String) {
        if (list.childCount <= 1) return toast(warning)
        list.removeView(row)
        scheduleConfigAutosave()
    }

    private fun setSegmentsEnabled(list: LinearLayout, enabled: Boolean) {
        for (i in 0 until list.childCount) {
            val row = list.getChildAt(i) as? LinearLayout ?: continue
            for (j in 0 until row.childCount) {
                val child = row.getChildAt(j)
                if (child is Spinner || child is MaterialButton) child.isEnabled = enabled
            }
        }
    }

    private fun collectSegments(list: LinearLayout, isoDay: Int?): List<String> {
        val out = mutableListOf<String>()
        for (i in 0 until list.childCount) {
            val row = list.getChildAt(i) as? LinearLayout ?: continue
            if (row.tag != TAG_SEGMENT) continue
            val start = row.findViewWithTag<Spinner>(TAG_START)?.selectedItem?.toString() ?: continue
            val end = row.findViewWithTag<Spinner>(TAG_END)?.selectedItem?.toString() ?: continue
            val (s, e) = clampRange(start, end, isoDay)
            out += "$s-$e"
        }
        return out
    }

    private fun simpleSelection(action: () -> Unit) = object : android.widget.AdapterView.OnItemSelectedListener {
        override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) = action()
        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
    }

    /** 选中 value；不在档位上时向下对齐到 ≤ value 的最大档位，与网页端一致。 */
    private fun Spinner.selectValue(value: String) {
        var target = 0
        for (i in 0 until adapter.count) {
            val item = adapter.getItem(i)?.toString() ?: continue
            if (item == value) { target = i; break }
            if (item < value) target = i
        }
        if (adapter.count > 0) setSelection(target)
    }

    private fun jsonKeys(obj: JSONObject): List<String> = obj.keys().asSequence().toList()
    private fun sortedZones(): List<String> = jsonKeys(seats).sortedWith(compareBy<String>(
        { ZONE_ORDER.indexOf(it).takeIf { index -> index >= 0 } ?: ZONE_ORDER.size },
        { it },
    ))
    private fun jsonArrayStrings(array: JSONArray?): List<String> = (0 until (array?.length() ?: 0)).mapNotNull { array?.optString(it)?.takeIf(String::isNotBlank) }
    private fun valueSegments(value: Any?): List<String> = when (value) {
        is JSONArray -> jsonArrayStrings(value).filter { SEGMENT_PATTERN.matches(it) }
        is String -> if (SEGMENT_PATTERN.matches(value)) listOf(value) else emptyList()
        else -> emptyList()
    }
    private fun timeOptions(min: String, max: String): List<String> {
        val lo = minutesOf(min); val hi = minutesOf(max)
        return (lo..hi step 30).map { "%02d:%02d".format(it / 60, it % 60) }
    }
    private fun minutesOf(value: String) = value.substringBefore(':').toIntOrNull()?.times(60)
        ?.plus(value.substringAfter(':').toIntOrNull() ?: 0) ?: 0
    private fun minutesBetween(start: String, end: String) = minutesOf(end) - minutesOf(start)
    private fun dayMax(isoDay: Int?) = if (isoDay == 5) "20:00" else "22:00"
    private fun clampRange(start: String, end: String, isoDay: Int?): Pair<String, String> {
        val max = dayMax(isoDay)
        val s = start.coerceIn("08:00", max)
        val e = end.coerceIn("08:00", max)
        return if (s >= e) "08:00" to max else s to e
    }
    private fun isoOf(calendar: Calendar) =
        calendar.get(Calendar.DAY_OF_WEEK).let { if (it == Calendar.SUNDAY) 7 else it - 1 }
    private fun isoToday() = isoOf(Calendar.getInstance())
    /** 周五闭馆早，结束时间一律收到 20:00，与网页端 friCap 一致。 */
    private fun friCap(segment: String): String {
        val end = segment.substringAfter('-')
        return if (end > "20:00") "${segment.substringBefore('-')}-20:00" else segment
    }
    /** 某个星期几实际生效的时段（考虑统一时段模式和周五封顶）。 */
    private fun effectiveSegments(cfg: JSONObject, iso: Int): List<String> {
        val time = cfg.optJSONObject("time")
        val segments = if (cfg.optString("mode") == "tomorrow") {
            valueSegments(time?.opt("tomorrow")).ifEmpty { listOf("08:00-22:00") }
        } else {
            valueSegments(time?.optJSONObject("week_time")?.opt(iso.toString()))
        }
        return if (iso == 5) segments.map(::friCap) else segments
    }
    private fun segmentLabel(segments: List<String>) = when {
        segments.isEmpty() -> "—"
        segments.size > 1 -> "${segments.size}段: ${segments.joinToString(" · ")}"
        else -> segments[0]
    }
    private fun hourRange(segment: String): Pair<Float, Float> =
        minutesOf(segment.substringBefore('-')) / 60f to minutesOf(segment.substringAfter('-')) / 60f
    private fun todayStamp() = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
    private fun findReservation(list: JSONArray, dayOffset: Int): JSONObject? {
        val calendar = Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, dayOffset) }
        val prefix = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(calendar.time)
        return (0 until list.length()).mapNotNull { list.optJSONObject(it) }.firstOrNull { it.optString("resvBeginTime").startsWith(prefix) }
    }
    private fun reservationStatus(code: Int) = mapOf(
        1027 to "已预约", 1093 to "使用中", 1169 to "已违约",
        3141 to "暂离", 3265 to "已结束", 3281 to "已违约",
    )[code] ?: "状态 $code"
    private fun announcementColor(level: String) = when (level) {
        "success" -> R.color.success
        "danger" -> R.color.danger
        "warning" -> R.color.primary_variant
        else -> R.color.primary
    }
    private fun accountExists(pid: String) = accountSummary(pid) != null
    private fun accountSummary(pid: String): JSONObject? = (0 until accounts.length())
        .mapNotNull { accounts.optJSONObject(it) }
        .firstOrNull { it.optString("pid") == pid }
    private fun formatMinutes(minutes: Int) = if (minutes >= 60) "${minutes / 60}小时${minutes % 60}分" else "${minutes}分钟"
    private fun defaultNapConfig() = JSONObject()
        .put("start_time", "14:00").put("end_time", "").put("seat", "")
        .put("auto_daily", false).put("trigger_time", "12:00")
    private fun copyNapConfig() = JSONObject()
        .put("start_time", napConfig.optString("start_time").ifBlank { "14:00" })
        .put("end_time", napConfig.optString("end_time"))
        .put("seat", napConfig.optString("seat"))
        .put("auto_daily", napConfig.optBoolean("auto_daily"))
        .put("trigger_time", napConfig.optString("trigger_time").ifBlank { "12:00" })
    private fun defaultWeekTime(): JSONObject {
        val week = JSONObject()
        WEEK_DEFAULTS.forEachIndexed { index, segment -> week.put((index + 1).toString(), JSONArray().put(segment)) }
        return JSONObject().put("week_time", week)
    }
    // endregion

    companion object {
        private const val PAGE_HOME = 1
        private const val PAGE_CONFIG = 2
        private const val PAGE_SETTINGS = 3
        private const val STATUS_IN_USE = 1093
        private const val STATUS_AWAY = 3141
        private const val STATUS_FINISHED = 3265
        private const val TAG_SEGMENT = "seg"
        private const val TAG_START = "start"
        private const val TAG_END = "end"
        private const val PREF_LP_ACK = "late_protection_ack"
        /** 欢迎介绍的「已读」标记。加了大功能就把 v1 递增，让老用户再看一次。 */
        private const val PREF_WELCOME_ACK = "welcome_ack_v1"
        private const val REQUEST_NOTIFICATIONS = 3001
        private const val PREF_NOTIFY_ASKED = "notify_permission_asked"
        private const val STATE_PAGE = "page"
        private const val STATE_RESERVATIONS_AT = "reservations_at"
        private const val STATE_RESERVATIONS_PID = "reservations_pid"
        private const val STATE_TODAY = "today_reservation"
        private const val STATE_TOMORROW = "tomorrow_reservation"
        /** 预约结果的复用窗口：切页返回不再重查，超过才自动刷新。 */
        private const val RESERVATION_TTL_MS = 3 * 60 * 1000L
        private const val PREF_THEME = "theme"
        private const val THEME_SYSTEM = "system"
        private const val THEME_LIGHT = "light"
        private const val THEME_DARK = "dark"
        private val THEME_LABELS = mapOf(
            THEME_SYSTEM to "跟随系统", THEME_LIGHT to "亮色", THEME_DARK to "暗色",
        )
        private const val WELCOME_RESERVE =
            "填好学号、想坐哪、几点到几点，剩下的交给我。可以按星期分别排，" +
                "也可以每天固定时段。抢完发通知告诉你结果。(｡･∀･)ﾉﾞ"
        private const val WELCOME_LATE_PROTECTION =
            "没按时到馆？系统自动把预约推迟 1 小时，座位先给你留着。" +
                "到了记得点主页的「我已到馆」。推迟后再不来，就按学校规则算违约。( ‵▽′)ψ"
        private const val WELCOME_NAP =
            "出门吃饭前点一下，系统立刻退掉当前预约、用同一个座位重新约下午时段，" +
                "回来座位还在。也可以设成每天自动执行。🍚(*´∀`)~♪"
        private const val WELCOME_WIDGET =
            "长按桌面添加小组件，2×2 / 4×2 / 4×4 三种规格，不打开 App 也能看今天坐哪、" +
                "点「我已到馆」。主页还能看按学期统计的自习热力图。"
        private const val LATE_PROTECTION_INFO =
            "开启后，系统会在你预约开始前检查是否到馆。\n\n" +
                "· 最多保护 1 小时：未按时到馆则自动把预约推迟 1 小时为你保留座位\n" +
                "· 到馆后手动确认：请点击主页的「我已到馆」按钮避免误操作\n" +
                "· 1 小时后仍未到：系统将自动释放预约，杜绝恶意占座"
        private const val NAP_INFO =
            "专为午休设计的快捷功能，出门吃饭前点一下，回来时座位还在。\n\n" +
                "· 自动续约下午：系统立即取消当前预约，并以相同座位重新预约下午时段（默认 14:00 起）\n" +
                "· 每日自动触发：在设置页开启后，每天到触发时刻（默认 12:00）自动执行\n" +
                "· 极小占座风险：取消到重新预约约需 1 秒，极低概率被他人抢占"
        private val ACTIVE_STATUSES = setOf(1027, 1093, 3141)
        private val BREACHED_STATUSES = setOf(1169, 3281)
        private val DAY_LABELS = listOf("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        private val DAY_SHORT = listOf("一", "二", "三", "四", "五", "六", "日")
        private val WEEK_SHORT = listOf("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
        private val WEEK_DEFAULTS = listOf(
            "08:00-22:00", "08:00-22:00", "08:00-22:00", "08:00-22:00",
            "08:00-20:00", "08:00-22:00", "08:00-22:00",
        )
        private val ZONE_ORDER = listOf(
            "二楼A区", "二楼B区", "六楼A区", "七楼A区", "七楼B区", "三楼夹层",
            "三楼A区", "三楼B区", "三楼C区", "四楼夹层", "四楼A区", "五楼A区",
        )
        private val SEGMENT_PATTERN = Regex("""^\d\d:\d\d-\d\d:\d\d$""")
    }
}
