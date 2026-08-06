package com.autolib.app

import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
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
    private var auth = JSONObject().put("is_guest", true)
    private var accounts = JSONArray()
    private var seats = JSONObject()
    private var currentPid = ""
    private var currentConfig: JSONObject? = null
    private var todayReservation: JSONObject? = null
    private var tomorrowReservation: JSONObject? = null
    private var currentPage = PAGE_HOME

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        api = NativeApi(this)
        setupNavigation()
        binding.accountButton.setOnClickListener { showAccountChooser() }
        loadInitialData()
    }

    private fun setupNavigation() {
        binding.navigation.menu.apply {
            add(0, PAGE_HOME, 0, "主页").setIcon(R.drawable.ic_home_native)
            add(0, PAGE_CONFIG, 1, "配置").setIcon(R.drawable.ic_config_native)
            add(0, PAGE_SETTINGS, 2, "设置").setIcon(R.drawable.ic_settings_native)
        }
        binding.navigation.setOnItemSelectedListener { item ->
            currentPage = item.itemId
            renderCurrentPage(refresh = currentPage == PAGE_HOME)
            true
        }
        binding.navigation.selectedItemId = PAGE_HOME
    }

    private fun loadInitialData() {
        setBusy(true)
        api.get("/api/auth/me") { me ->
            auth = me.jsonObject ?: JSONObject().put("is_guest", true)
            val seatPath = if (auth.optBoolean("logged_in")) "/api/seats" else "/api/public/seats"
            api.get(seatPath) { seatResponse ->
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
                !saved.isNullOrBlank() && accountExists(saved) -> saved
                accounts.length() > 0 -> accounts.optJSONObject(0)?.optString("pid").orEmpty()
                else -> ""
            }
            if (currentPid.isBlank()) {
                currentConfig = null
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
            currentConfig = response.jsonObject?.takeIf { it.length() > 0 }
            getPreferences(MODE_PRIVATE).edit().putString("current_pid", pid).apply()
            setBusy(false)
            updateHeader()
            after?.invoke() ?: renderCurrentPage(refresh = currentPage == PAGE_HOME)
        }
    }

    private fun renderCurrentPage(refresh: Boolean = false) {
        when (currentPage) {
            PAGE_CONFIG -> renderConfig()
            PAGE_SETTINGS -> renderSettings()
            else -> renderHome(refresh)
        }
    }

    private fun updateHeader() {
        val guest = !auth.optBoolean("logged_in")
        val displayName = if (guest) "同学" else auth.optString("nickname").ifBlank { auth.optString("uid") }
        binding.title.text = "你好，$displayName"
        binding.subtitle.text = SimpleDateFormat("EEE · MM月dd日", Locale.CHINA).format(Date())
        binding.accountButton.text = currentPid.ifBlank { "添加学号" }
    }

    // region Home
    private fun renderHome(refresh: Boolean) {
        val host = pageHost()
        host.addView(section("今日预约"))
        val todayCard = card()
        styleHeroCard(todayCard, tomorrow = false)
        host.addView(todayCard)
        host.addView(section("明日预约"))
        val tomorrowCard = card()
        styleHeroCard(tomorrowCard, tomorrow = true)
        host.addView(tomorrowCard)
        host.addView(section("通知"))
        val notices = vertical()
        host.addView(notices)

        if (currentConfig == null) {
            replaceCard(todayCard, vertical(0).apply {
                addView(heroHeader(false))
                addView(text("还没有学号", 22, true))
                addView(text("添加并验证图书馆账号后，才能查询和预约座位。", 14))
                addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() })
            })
            replaceCard(tomorrowCard, vertical(0).apply { addView(heroHeader(true)); addView(text("暂无配置", 18, true)) })
            loadNotices(notices)
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
                renderReservationCard(todayCard, todayReservation, false)
                renderReservationCard(tomorrowCard, tomorrowReservation, true)
            }
        }
        loadNotices(notices)
    }

    private fun renderReservationCard(target: MaterialCardView, reservation: JSONObject?, tomorrow: Boolean) {
        target.removeAllViews()
        val content = vertical(dp(18))
        target.addView(content)
        content.addView(heroHeader(tomorrow))
        if (reservation == null) {
            content.addView(text(if (tomorrow) "明日暂无预约" else "今日暂无预约", 19, true))
            val running = currentConfig?.optString("is_reserved") == "True"
            content.addView(text(if (running) "自动预约已开启" else "自动预约已暂停", 14))
            if (!tomorrow) content.addView(action("⚡ 立即预约", accent = true) { showReserveDialog() })
            return
        }
        val seat = reservation.optJSONObject("devInfo")?.optString("devName").orEmpty().ifBlank { "未知座位" }
        val begin = reservation.optString("resvBeginTime").substringAfter(' ', "").take(5)
        val end = reservation.optString("resvEndTime").substringAfter(' ', "").take(5)
        val status = reservationStatus(reservation.optInt("resvStatus", -1))
        content.addView(text(seat, 38, true).apply {
            setTextColor(color(if (tomorrow) R.color.tomorrow else R.color.primary))
            typeface = Typeface.create("sans-serif-rounded", Typeface.BOLD)
            letterSpacing = -0.025f
        })
        content.addView(text("$begin  —  $end", 18, true).apply { setTextColor(color(R.color.text_secondary)) })
        content.addView(statusPill(status, if (tomorrow) R.color.tomorrow else R.color.success,
            if (tomorrow) R.color.tomorrow_soft else R.color.success_soft))
        if (!tomorrow && reservation.optInt("resvStatus") in ACTIVE_STATUSES) {
            val row = horizontal()
            row.addView(action("我已到馆", 1f, accent = true) { toggleArrived() })
            row.addView(action("午休", 1f) { showNapDialog(reservation) })
            row.addView(action("取消", 1f, danger = true) { confirmCancel(reservation) })
            content.addView(row)
        }
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
                    host.addView(noticeCard("公告 · ${item.optString("title")}", item.optString("content")))
                }
                for (i in 0 until results.length()) {
                    val item = results.optJSONObject(i) ?: continue
                    host.addView(noticeCard("学号 ${item.optString("pid")} · ${if (item.optBoolean("success")) "预约成功" else "预约结果"}", item.optString("result")))
                }
                if (host.childCount == 0) host.addView(text("暂无通知", 14))
            }
        }
    }

    private fun showReserveDialog() {
        if (currentPid.isBlank()) return showAddAccountDialog()
        if (seats.length() == 0) return toast("座位列表尚未加载")
        val body = vertical()
        val zone = spinner(jsonKeys(seats))
        val seat = spinner(emptyList())
        fun updateSeats() {
            val values = jsonArrayStrings(seats.optJSONArray(zone.selectedItem?.toString()))
            seat.adapter = arrayAdapter(values)
        }
        zone.onItemSelectedListener = simpleSelection { updateSeats() }
        val start = spinner(timeOptions("08:00", "20:00"))
        val end = spinner(timeOptions("10:00", "22:00")).apply { setSelection(adapter.count - 1) }
        body.addView(labeled("区域", zone)); body.addView(labeled("座位", seat))
        body.addView(labeled("开始时间", start)); body.addView(labeled("结束时间", end))
        val dialog = AlertDialog.Builder(this).setTitle("立即预约今日座位").setView(body)
            .setNegativeButton("取消", null).setPositiveButton("预约", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val startValue = start.selectedItem?.toString().orEmpty()
                val endValue = end.selectedItem?.toString().orEmpty()
                if (startValue >= endValue || minutesBetween(startValue, endValue) < 120) {
                    toast("结束时间须晚于开始时间，且至少预约 2 小时")
                    return@setOnClickListener
                }
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                api.post("/api/my/accounts/${api.encoded(currentPid)}/reserve_custom", JSONObject()
                    .put("seat", seat.selectedItem?.toString().orEmpty())
                    .put("start_time", startValue).put("end_time", endValue)) { response ->
                    toast(response.jsonObject?.optString("result").orEmpty().ifBlank { response.message("预约请求已完成") })
                    if (response.ok) { dialog.dismiss(); renderHome(refresh = true) }
                    else dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                }
            }
        }
        dialog.show()
    }

    private fun confirmCancel(reservation: JSONObject) {
        AlertDialog.Builder(this).setTitle("取消今日预约？").setMessage("取消后座位可能立即被其他人预约。")
            .setNegativeButton("返回", null).setPositiveButton("确认取消") { _, _ ->
                setBusy(true)
                api.post("/api/my/accounts/${api.encoded(currentPid)}/cancel", JSONObject().put("uuid", reservation.optString("uuid"))) { response ->
                    setBusy(false); toast(response.message("取消请求已完成")); renderHome(refresh = true)
                }
            }.show()
    }

    private fun toggleArrived() {
        setBusy(true)
        api.post("/api/my/accounts/${api.encoded(currentPid)}/arrived") { response ->
            setBusy(false)
            toast(if (response.jsonObject?.optBoolean("arrived") == true) "已标记到馆" else response.message("已更新"))
            loadAccountDetail(currentPid) { renderHome(refresh = true) }
        }
    }

    private fun showNapDialog(reservation: JSONObject) {
        val seat = reservation.optJSONObject("devInfo")?.optString("devName").orEmpty()
        val end = reservation.optString("resvEndTime").substringAfter(' ', "").take(5)
        val body = vertical()
        val start = spinner(timeOptions("12:00", "20:00")).apply { selectValue("14:00") }
        body.addView(text("系统会先取消当前预约，再预约同一座位的下午时段。中间存在短暂被抢占风险。", 14))
        body.addView(labeled("回来时间", start))
        AlertDialog.Builder(this).setTitle("午休续约 · $seat").setView(body).setNegativeButton("取消", null)
            .setPositiveButton("继续") { _, _ ->
                val startValue = start.selectedItem.toString()
                if (startValue >= end) return@setPositiveButton toast("回来时间必须早于当前预约结束时间")
                setBusy(true)
                api.post("/api/my/accounts/${api.encoded(currentPid)}/nap", JSONObject()
                    .put("uuid", reservation.optString("uuid")).put("seat", seat)
                    .put("start_time", startValue).put("end_time", end)) { response ->
                    setBusy(false)
                    toast(response.jsonObject?.optString("result").orEmpty().ifBlank { response.message("午休操作已完成") })
                    renderHome(refresh = true)
                }
            }.show()
    }
    // endregion

    // region Config
    private data class DayControls(val enabled: SwitchMaterial, val start: Spinner, val end: Spinner)

    private fun renderConfig() {
        val host = pageHost()
        host.addView(section("每日预约规则"))
        val cfg = currentConfig
        if (cfg == null) {
            host.addView(card().apply {
                addView(vertical(dp(18)).apply { addView(text("请先添加学号", 20, true)); addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() }) })
            })
            return
        }
        val modeGroup = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val weekRadio = RadioButton(this).apply { text = "按星期"; id = View.generateViewId() }
        val simpleRadio = RadioButton(this).apply { text = "统一时段"; id = View.generateViewId() }
        modeGroup.addView(weekRadio); modeGroup.addView(simpleRadio)
        val isWeek = cfg.optString("mode", "week_time") == "week_time"
        modeGroup.check(if (isWeek) weekRadio.id else simpleRadio.id)
        host.addView(cardBlock("时间模式", modeGroup))

        val timeCard = card()
        val timeContent = vertical(dp(18)).apply { addView(text("一周时间表", 16, true)) }
        timeCard.addView(timeContent); host.addView(timeCard)
        val dayControls = mutableListOf<DayControls>()
        val simpleControls = arrayOf(spinner(timeOptions("08:00", "21:30")), spinner(timeOptions("08:30", "22:00")))

        fun renderTimeControls(weekMode: Boolean) {
            timeContent.removeAllViews(); timeContent.addView(text(if (weekMode) "一周时间表" else "每日固定时段", 16, true)); dayControls.clear()
            if (weekMode) {
                val weekJson = cfg.optJSONObject("time")?.optJSONObject("week_time")
                DAY_LABELS.forEachIndexed { index, label ->
                    val day = index + 1
                    val segments = valueSegments(weekJson?.opt(day.toString()))
                    val enabled = SwitchMaterial(this).apply { text = label; isChecked = segments.isNotEmpty() }
                    val max = if (day == 5) "20:00" else "22:00"
                    val start = spinner(timeOptions("08:00", max)).apply { selectValue(segments.firstOrNull()?.substringBefore('-') ?: "08:00") }
                    val end = spinner(timeOptions("08:30", max)).apply { selectValue(segments.firstOrNull()?.substringAfter('-') ?: max) }
                    val row = horizontal().apply {
                        addView(enabled, LinearLayout.LayoutParams(0, dp(52), 1.3f)); addView(start, LinearLayout.LayoutParams(0, dp(52), 1f))
                        addView(text("—", 14).apply { gravity = Gravity.CENTER }, LinearLayout.LayoutParams(dp(24), dp(52)))
                        addView(end, LinearLayout.LayoutParams(0, dp(52), 1f))
                    }
                    fun sync() { start.isEnabled = enabled.isChecked; end.isEnabled = enabled.isChecked }
                    enabled.setOnCheckedChangeListener { _, _ -> sync() }; sync()
                    timeContent.addView(row); dayControls += DayControls(enabled, start, end)
                }
            } else {
                val segment = valueSegments(cfg.optJSONObject("time")?.opt("tomorrow")).firstOrNull() ?: "08:00-22:00"
                simpleControls[0].selectValue(segment.substringBefore('-')); simpleControls[1].selectValue(segment.substringAfter('-'))
                timeContent.addView(labeled("开始时间", simpleControls[0])); timeContent.addView(labeled("结束时间", simpleControls[1]))
            }
        }
        renderTimeControls(isWeek)
        modeGroup.setOnCheckedChangeListener { _, checked -> renderTimeControls(checked == weekRadio.id) }

        val chosenSeats = jsonArrayStrings(cfg.optJSONArray("seat_list")).toMutableList()
        val seatList = vertical(0)
        fun renderChosenSeats() {
            seatList.removeAllViews()
            chosenSeats.forEachIndexed { index, name ->
                val row = horizontal(); row.addView(text("${index + 1}. $name", 15), LinearLayout.LayoutParams(0, dp(48), 1f))
                row.addView(action("移除") { chosenSeats.removeAt(index); renderChosenSeats() })
                seatList.addView(row)
            }
            seatList.addView(action("＋ 添加候选座位", accent = true) { pickSeat { chosenSeats += it; renderChosenSeats() } })
        }
        renderChosenSeats(); host.addView(cardBlock("座位优先级", seatList))

        val autoReserve = SwitchMaterial(this).apply { text = "自动预约"; isChecked = cfg.optString("is_reserved") == "True" }
        val lateProtection = SwitchMaterial(this).apply { text = "迟到保护"; isChecked = cfg.optString("late_protection") == "True" }
        host.addView(cardBlock("功能开关", vertical(0).apply { addView(autoReserve); addView(lateProtection) }))

        val vpn = input("统一身份认证密码", password = true).apply { setText(cfg.optString("vpn_password")) }
        host.addView(cardBlock("账号凭据", vertical(0).apply { addView(vpn) }))

        val saveRow = horizontal()
        saveRow.addView(action("验证并保存", 1f, accent = true) {
            if (vpn.text.isBlank()) return@action toast("请填写统一身份认证密码")
            setBusy(true)
            api.post("/api/my/accounts/${api.encoded(currentPid)}/verify", JSONObject()
                .put("vpn_password", vpn.text.toString())) { response ->
                if (response.jsonObject?.optBoolean("verified") == true) {
                    saveConfiguration(modeGroup.checkedRadioButtonId == weekRadio.id, dayControls, simpleControls,
                        chosenSeats, autoReserve.isChecked, lateProtection.isChecked, vpn.text.toString(), true)
                } else { setBusy(false); toast(response.message("验证失败")) }
            }
        })
        saveRow.addView(action("仅保存", 1f) {
            saveConfiguration(modeGroup.checkedRadioButtonId == weekRadio.id, dayControls, simpleControls,
                chosenSeats, autoReserve.isChecked, lateProtection.isChecked, vpn.text.toString(), false)
        })
        host.addView(saveRow)
        host.addView(action("⚡ 立即预约已配置的首选座位", accent = true) {
            setBusy(true); api.post("/api/my/accounts/${api.encoded(currentPid)}/reserve_now") { response ->
                setBusy(false); toast(response.jsonObject?.optString("result").orEmpty().ifBlank { response.message("执行完成") })
            }
        })
    }

    private fun saveConfiguration(
        weekMode: Boolean, days: List<DayControls>, simple: Array<Spinner>, chosenSeats: List<String>,
        autoReserve: Boolean, lateProtection: Boolean, vpn: String, verified: Boolean,
    ) {
        if (chosenSeats.isEmpty()) { setBusy(false); return toast("至少添加一个候选座位") }
        val time = JSONObject()
        if (weekMode) {
            val week = JSONObject()
            days.forEachIndexed { index, day ->
                if (!day.enabled.isChecked) week.put((index + 1).toString(), "休息")
                else {
                    val start = day.start.selectedItem.toString(); val end = day.end.selectedItem.toString()
                    if (start >= end) { setBusy(false); return toast("${DAY_LABELS[index]}的结束时间必须晚于开始时间") }
                    week.put((index + 1).toString(), JSONArray().put("$start-$end"))
                }
            }
            time.put("week_time", week)
        } else {
            val start = simple[0].selectedItem.toString(); val end = simple[1].selectedItem.toString()
            if (start >= end) { setBusy(false); return toast("结束时间必须晚于开始时间") }
            time.put("tomorrow", JSONArray().put("$start-$end"))
        }
        val body = JSONObject().put("mode", if (weekMode) "week_time" else "tomorrow")
            .put("time", time).put("seat_list", JSONArray(chosenSeats))
            .put("is_reserved", if (autoReserve) "True" else "False")
            .put("late_protection", if (lateProtection) "True" else "False")
            .put("vpn_password", vpn)
        if (verified) body.put("verified", true)
        setBusy(true)
        api.post("/api/my/accounts/${api.encoded(currentPid)}", body) { response ->
            setBusy(false); toast(response.message(if (verified) "验证并保存成功" else "配置已保存"))
            if (response.ok) loadInitialData()
        }
    }
    // endregion

    // region Settings and accounts
    private fun renderSettings() {
        val host = pageHost()
        host.addView(section("账号"))
        val loggedIn = auth.optBoolean("logged_in")
        val accountBody = vertical(0)
        accountBody.addView(text(if (loggedIn) auth.optString("nickname").ifBlank { auth.optString("uid") } else "游客模式", 20, true))
        accountBody.addView(text("${accounts.length()} 个图书馆学号", 14))
        val authRow = horizontal()
        if (loggedIn) {
            authRow.addView(action("编辑资料", 1f) { showProfileDialog() })
            authRow.addView(action("退出登录", 1f, danger = true) { logout() })
        } else {
            authRow.addView(action("登录", 1f, accent = true) { showLoginDialog() })
        }
        accountBody.addView(authRow); host.addView(cardBlock("AutoLib 身份", accountBody))

        val libraryBody = vertical(0)
        for (i in 0 until accounts.length()) {
            val item = accounts.optJSONObject(i) ?: continue
            val pid = item.optString("pid")
            val row = horizontal()
            row.addView(text(if (pid == currentPid) "✓ $pid" else pid, 16, pid == currentPid), LinearLayout.LayoutParams(0, dp(48), 1f))
            row.addView(action("切换") { switchAccount(pid) }); row.addView(action("删除", danger = true) { confirmDeleteAccount(pid) })
            libraryBody.addView(row)
        }
        libraryBody.addView(action("＋ 添加学号", accent = true) { showAddAccountDialog() })
        host.addView(cardBlock("图书馆学号", libraryBody))

        host.addView(section("学习记录"))
        val stats = card(); stats.addView(vertical(dp(18)).apply { addView(text("加载中…", 14)) }); host.addView(stats)
        api.get("/api/my/visit_stats") { response ->
            val data = response.jsonObject ?: JSONObject()
            val content = vertical(0)
            content.addView(text("本周 ${data.optInt("this_week_visits")} 次 · ${formatMinutes(data.optInt("this_week_minutes"))}", 19, true))
            content.addView(text("累计 ${data.optInt("total_visits")} 次 · ${formatMinutes(data.optInt("total_minutes"))}", 15))
            val recent = data.optJSONArray("recent") ?: JSONArray()
            for (i in 0 until recent.length()) {
                val item = recent.optJSONObject(i) ?: continue
                content.addView(text("${item.optString("date")}  ${item.optString("location").ifBlank { item.optString("seat_name") }}  ${formatMinutes(item.optInt("duration_minutes"))}", 13))
            }
            replaceCard(stats, content)
        }
    }

    private fun showLoginDialog() {
        val body = vertical(); val username = input("学号"); val password = input("统一身份认证密码", true)
        body.addView(text("无需注册：使用学号 + 统一身份认证（网上办事大厅）密码登录，验证通过即完成登录并绑定。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        body.addView(username); body.addView(password)
        AlertDialog.Builder(this).setTitle("登录 AutoLib").setView(body)
            .setNegativeButton("取消", null).setPositiveButton("登录") { _, _ ->
                setBusy(true)
                api.post("/api/auth/login", JSONObject()
                    .put("username", username.text.toString().trim()).put("password", password.text.toString())) { response ->
                    setBusy(false); toast(response.message("登录成功"))
                    if (response.ok) loadInitialData()
                }
            }.show()
    }

    private fun showProfileDialog() {
        val body = vertical(); val nickname = input("昵称").apply { setText(auth.optString("nickname")) }
        body.addView(nickname)
        body.addView(text("密码即统一身份认证密码，需在学校的系统里修改。", 13)
            .apply { setTextColor(color(R.color.text_secondary)) })
        AlertDialog.Builder(this).setTitle("编辑资料").setView(body).setNegativeButton("取消", null).setPositiveButton("保存") { _, _ ->
            val data = JSONObject().put("nickname", nickname.text.toString().trim())
            api.post("/api/auth/profile", data) { response -> toast(response.message("已更新")); if (response.ok) loadInitialData() }
        }.show()
    }

    private fun logout() {
        AlertDialog.Builder(this).setTitle("退出登录？").setMessage("未绑定到账户的游客会话数据可能无法再次访问。")
            .setNegativeButton("取消", null).setPositiveButton("退出") { _, _ -> api.post("/api/auth/logout") { loadInitialData() } }.show()
    }

    private fun showAccountChooser() {
        if (accounts.length() == 0) return showAddAccountDialog()
        val options = (0 until accounts.length()).map { accounts.optJSONObject(it)?.optString("pid").orEmpty() } + "＋ 添加学号"
        AlertDialog.Builder(this).setTitle("选择图书馆学号").setItems(options.toTypedArray()) { _, index ->
            if (index == options.lastIndex) showAddAccountDialog() else switchAccount(options[index])
        }.show()
    }

    private fun switchAccount(pid: String) {
        currentPid = pid; setBusy(true); loadAccountDetail(pid)
    }

    private fun showAddAccountDialog() {
        val body = vertical(); val pid = input("学号"); val vpn = input("统一身份认证密码", true)
        body.addView(pid); body.addView(vpn)
        val dialog = AlertDialog.Builder(this).setTitle("添加图书馆学号").setView(body).setNegativeButton("取消", null)
            .setPositiveButton("验证并保存", null).create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val p = pid.text.toString().trim()
                if (p.isBlank() || vpn.text.isBlank()) return@setOnClickListener toast("请填写学号和统一身份认证密码")
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                setBusy(true)
                api.post("/api/my/accounts/${api.encoded(p)}/verify", JSONObject()
                    .put("vpn_password", vpn.text.toString())) { verify ->
                    if (verify.jsonObject?.optBoolean("verified") != true) {
                        setBusy(false); dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true; toast(verify.message("验证失败"))
                    } else {
                        api.post("/api/my/accounts/${api.encoded(p)}", JSONObject().put("vpn_password", vpn.text.toString())
                            .put("mode", "week_time").put("verified", true)) { saved ->
                            setBusy(false); toast(saved.message("学号已保存"))
                            if (saved.ok) {
                                dialog.dismiss()
                                getPreferences(MODE_PRIVATE).edit().putString("current_pid", p).apply()
                                loadInitialData()
                            } else dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
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
                api.delete("/api/my/accounts/${api.encoded(pid)}") { response -> toast(response.message("已删除")); if (response.ok) loadAccounts() }
            }.show()
    }
    // endregion

    // region UI helpers
    private fun pageHost(): LinearLayout {
        binding.content.removeAllViews()
        val scroll = ScrollView(this).apply { isFillViewport = true }
        val host = vertical(0).apply { setPadding(dp(18), 0, dp(18), dp(30)) }
        scroll.addView(host); binding.content.addView(scroll)
        return host
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
    private fun noticeCard(title: String, body: String) = card().apply {
        strokeColor = color(R.color.primary)
        addView(vertical(dp(15)).apply {
            addView(text(title, 15, true).apply { setTextColor(color(R.color.primary)) })
            addView(text(body, 13).apply { setTextColor(color(R.color.text_secondary)) })
        })
    }
    private fun action(
        label: String,
        weight: Float? = null,
        danger: Boolean = false,
        accent: Boolean = false,
        onClick: () -> Unit,
    ) = MaterialButton(this).apply {
        text = label; isAllCaps = false; textSize = 14f; setOnClickListener { onClick() }
        typeface = Typeface.create("sans-serif-rounded", Typeface.BOLD)
        cornerRadius = dp(10); strokeWidth = dp(2)
        backgroundTintList = ColorStateList.valueOf(color(if (accent) R.color.primary else R.color.surface))
        strokeColor = ColorStateList.valueOf(color(if (danger) R.color.danger else if (accent) R.color.primary else R.color.stroke))
        setTextColor(color(if (danger) R.color.danger else if (accent) R.color.on_primary else R.color.text_primary))
        val params = if (weight == null) LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(48))
        else LinearLayout.LayoutParams(0, dp(48), weight)
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
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(30))
    }

    private fun pickSeat(selected: (String) -> Unit) {
        if (seats.length() == 0) return toast("没有可用座位数据")
        val body = vertical(); val zone = spinner(jsonKeys(seats)); val seat = spinner(emptyList())
        fun update() { seat.adapter = arrayAdapter(jsonArrayStrings(seats.optJSONArray(zone.selectedItem?.toString()))) }
        zone.onItemSelectedListener = simpleSelection { update() }
        body.addView(labeled("区域", zone)); body.addView(labeled("座位", seat))
        AlertDialog.Builder(this).setTitle("添加候选座位").setView(body).setNegativeButton("取消", null).setPositiveButton("添加") { _, _ ->
            seat.selectedItem?.toString()?.takeIf { it.isNotBlank() }?.let(selected)
        }.show()
    }

    private fun simpleSelection(action: () -> Unit) = object : android.widget.AdapterView.OnItemSelectedListener {
        override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) = action()
        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
    }
    private fun Spinner.selectValue(value: String) {
        for (i in 0 until adapter.count) if (adapter.getItem(i)?.toString() == value) { setSelection(i); break }
    }
    private fun jsonKeys(obj: JSONObject): List<String> = obj.keys().asSequence().toList().sorted()
    private fun jsonArrayStrings(array: JSONArray?): List<String> = (0 until (array?.length() ?: 0)).mapNotNull { array?.optString(it)?.takeIf(String::isNotBlank) }
    private fun valueSegments(value: Any?): List<String> = when (value) {
        is JSONArray -> jsonArrayStrings(value).filter { it.contains('-') }
        is String -> if (value.contains('-')) listOf(value) else emptyList()
        else -> emptyList()
    }
    private fun timeOptions(min: String, max: String): List<String> {
        fun parse(v: String) = v.substringBefore(':').toInt() * 60 + v.substringAfter(':').toInt()
        fun format(v: Int) = "%02d:%02d".format(v / 60, v % 60)
        return (parse(min)..parse(max) step 30).map(::format)
    }
    private fun minutesBetween(start: String, end: String): Int {
        fun parse(v: String) = v.substringBefore(':').toInt() * 60 + v.substringAfter(':').toInt()
        return parse(end) - parse(start)
    }
    private fun findReservation(list: JSONArray, dayOffset: Int): JSONObject? {
        val calendar = Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, dayOffset) }
        val prefix = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(calendar.time)
        return (0 until list.length()).mapNotNull { list.optJSONObject(it) }.firstOrNull { it.optString("resvBeginTime").startsWith(prefix) }
    }
    private fun reservationStatus(code: Int) = mapOf(1027 to "已预约", 1093 to "使用中", 1169 to "已违约", 3141 to "暂离", 3265 to "已结束", 3281 to "已违约")[code] ?: "状态 $code"
    private fun accountExists(pid: String) = (0 until accounts.length()).any { accounts.optJSONObject(it)?.optString("pid") == pid }
    private fun formatMinutes(minutes: Int) = if (minutes >= 60) "${minutes / 60}小时${minutes % 60}分" else "${minutes}分钟"
    // endregion

    companion object {
        private const val PAGE_HOME = 1
        private const val PAGE_CONFIG = 2
        private const val PAGE_SETTINGS = 3
        private val ACTIVE_STATUSES = setOf(1027, 1093, 3141)
        private val DAY_LABELS = listOf("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    }
}
