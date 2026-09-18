package com.autolib.app

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject

/**
 * 小组件「取消预约」的二次确认。
 *
 * 桌面上的 RemoteViews 弹不了对话框，所以用一个透明的 Activity 托一个 AlertDialog：
 * 看起来像从桌面直接弹出的确认框，不会把整个 App 拉起来跑一遍首屏加载。
 * 确认后在后台线程直接调 `/cancel`，成功就把缓存里今天的座位抹掉刷新三个组件。
 */
class WidgetCancelActivity : AppCompatActivity() {

    private var busy = false

    override fun onCreate(savedInstanceState: Bundle?) {
        MainActivity.applyStoredNightMode(this)
        super.onCreate(savedInstanceState)
        val s = ReservationCache.read(this)
        // 桌面上的按钮可能是几分钟前渲染的，这段时间里预约状态可能已经变了
        if (!s.isToday || !s.hasSeat || s.pid.isBlank() || s.uuid.isBlank()) {
            Toast.makeText(this, "请先打开 App 同步数据", Toast.LENGTH_SHORT).show()
            finish()
            return
        }
        val leaving = s.seated
        AlertDialog.Builder(this)
            .setTitle(if (leaving) "确认离馆？" else "取消今日预约？")
            .setMessage(
                (if (leaving) "离馆后座位立即释放，今天不能再用这次预约。临时外出请用「午休」保留座位。"
                else "取消后座位会释放。如果只是临时外出，可以不用取消。") +
                    "\n\n${s.seat} · ${s.begin} – ${s.end}"
            )
            .setNegativeButton("再想想") { _, _ -> finish() }
            .setPositiveButton(if (leaving) "确认离馆" else "确认取消") { _, _ -> cancel(s) }
            .setOnCancelListener { finish() }
            .show()
    }

    private fun cancel(s: ReservationCache.Snapshot) {
        if (busy) return
        busy = true
        val waiting = AlertDialog.Builder(this)
            .setMessage("正在取消预约…")
            .setCancelable(false)
            .show()
        val app = applicationContext
        Thread {
            val result = try {
                val api = NativeApi(app)
                val response = api.postBlocking(
                    "/api/my/accounts/${api.encoded(s.pid)}/cancel",
                    JSONObject().put("uuid", s.uuid).put("resv_status", s.statusCode),
                )
                if (response.ok && response.jsonObject?.optBoolean("success") == true) {
                    ReservationCache.clearToday(app)
                    response.jsonObject.optString("message").ifBlank { "已取消预约" }
                } else {
                    response.message("取消失败，请打开 App 重试")
                }
            } catch (_: Exception) {
                "网络异常，请打开 App 重试"
            }
            runOnUiThread {
                if (!isFinishing) waiting.dismiss()
                Toast.makeText(app, result, Toast.LENGTH_SHORT).show()
                finish()
            }
        }.start()
    }
}
