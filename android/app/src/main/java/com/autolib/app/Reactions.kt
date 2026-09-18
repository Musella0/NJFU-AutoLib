package com.autolib.app

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.AnimatorSet
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.app.Activity
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.text.BreakIterator
import kotlin.random.Random

/**
 * 公告栏小互动，对应网页端 app.js 的「公告栏小互动」一节。
 *
 * 每条公告可以在后台配一排表情按钮和一个应援按钮。表情按钮点了 emoji 会飞向屏幕中间
 * 砸一下，应援按钮点了会从按钮上方一起冒出几个 emoji。谁点的、点了几次记在后端
 * （`/api/reactions`），前端只显示总数，计数按「公告 + 表情」分开。
 *
 * 连点不逐次发请求：本地数字先跟着手指走，攒 [FLUSH_DELAY_MS] 再合成一包发出去。
 */
class Reactions(private val activity: Activity, private val api: NativeApi) {

    /** key = "annId|kind" → 显示给用户的总数（已含还没发出去的那几下）。 */
    private val totals = mutableMapOf<String, Int>()

    /** 还没发给后端的连点，发出去后清零。 */
    private val pending = mutableMapOf<String, Int>()

    /** 每个 key 对应界面上的那些计数控件；公告列表一重建就得清掉，旧的已经不在树上了。 */
    private val counters = mutableMapOf<String, MutableList<TextView>>()

    private val main = Handler(Looper.getMainLooper())
    private var flushScheduled = false

    /** 系统里把动画关掉了（开发者选项或省电）就别硬飞，对应网页端的 prefers-reduced-motion。 */
    private val animate: Boolean
        get() = Settings.Global.getFloat(
            activity.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f,
        ) != 0f

    /** 公告列表重建前调用：解绑上一批计数控件，留着会把 View 一直攥在手里。 */
    fun detachCounters() = counters.clear()

    /**
     * 这条公告的互动条。没配表情也没配应援按钮就回 null，卡片上不占位置。
     *
     * [item] 是 `/api/announcements` 里的一条，用到 `id` / `reactions` / `cheer`。
     */
    fun bar(item: JSONObject): View? {
        val annId = item.optString("id")
        val emojis = jsonStrings(item.optJSONArray("reactions"))
        val cheer = item.optJSONObject("cheer")
            ?.takeIf { it.optString("label").isNotBlank() && it.optString("emojis").isNotBlank() }
        if (annId.isBlank() || (emojis.isEmpty() && cheer == null)) return null

        return flowRow().apply {
            emojis.forEach { emoji -> addView(button(annId, emoji, emoji, null)) }
            cheer?.let {
                addView(button(annId, CHEER_KIND, it.optString("label"), it.optString("emojis")))
            }
        }
    }

    /** 拉一次全站总数。失败就静默——互动数字不该挡住公告本身。 */
    fun load() {
        api.get("/api/reactions") { response ->
            val serverTotals = response.jsonObject?.optJSONObject("totals") ?: return@get
            merge(serverTotals)
            paint()
        }
    }

    // ---------- 内部 ----------

    private fun key(annId: String, kind: String) = "$annId|$kind"

    /**
     * 一个互动按钮：表情（或应援文字）+ 计数。
     * [cheerEmojis] 只有应援按钮才有，是点下去要冒出来的那串表情。
     */
    private fun button(annId: String, kind: String, label: String, cheerEmojis: String?): View {
        val isCheer = cheerEmojis != null
        val counter = TextView(activity).apply {
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(color(R.color.text_muted))
            text = (totals[key(annId, kind)] ?: 0).toString()
        }
        counters.getOrPut(key(annId, kind)) { mutableListOf() }.add(counter)

        val face = TextView(activity).apply {
            text = label
            if (isCheer) {
                textSize = 13f
                typeface = Typeface.create("sans-serif-rounded", Typeface.BOLD)
                setTextColor(color(R.color.primary))
                letterSpacing = 0.04f
            } else {
                textSize = 17f
                setTextColor(color(R.color.text_primary))
            }
        }

        return LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(10), dp(6), dp(10), dp(6))
            background = GradientDrawable().apply {
                cornerRadius = dp(10).toFloat()
                setColor(color(R.color.surface))
                setStroke(dp(2), color(if (isCheer) R.color.primary else R.color.stroke))
            }
            addView(face)
            addView(counter, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { marginStart = dp(6) })
            isClickable = true
            setOnClickListener { view ->
                tap(annId, kind)
                if (isCheer) cheer(view, cheerEmojis.orEmpty()) else fly(view, label)
            }
        }
    }

    /**
     * 点一下：本地数字立刻走，请求攒着一起发。
     *
     * 这里是节流不是防抖——网页端每点一下都重置计时器，手一直不停就一直不发；
     * 改成从第一下起固定攒 [FLUSH_DELAY_MS] 就发一包，连点再久也不会饿着。
     * 后端收的本来就是增量 n，分几包发结果一样。
     */
    private fun tap(annId: String, kind: String) {
        val k = key(annId, kind)
        totals[k] = (totals[k] ?: 0) + 1
        pending[k] = (pending[k] ?: 0) + 1
        paint()
        if (flushScheduled) return
        flushScheduled = true
        main.postDelayed({ flushScheduled = false; flush() }, FLUSH_DELAY_MS)
    }

    private fun flush() {
        // 先把待发数取走再发请求：期间用户接着点，算进下一包，不会被回包抹掉
        val batch = pending.filterValues { it > 0 }
        batch.keys.forEach { pending[it] = 0 }
        batch.forEach { (k, n) ->
            val annId = k.substringBefore('|')
            val kind = k.substringAfter('|')
            val body = JSONObject()
                .put("ann_id", annId)
                .put("kind", kind)
                .put("n", minOf(n, MAX_PER_POST))
            api.post("/api/reactions", body) { response ->
                val serverTotals = response.jsonObject?.optJSONObject("totals") ?: return@post
                merge(serverTotals)
                paint()
            }
        }
    }

    /** 后端是准的（别人也在点），但本地还没发出去的那几下要补回来。 */
    private fun merge(serverTotals: JSONObject) {
        serverTotals.keys().forEach { annId ->
            val kinds = serverTotals.optJSONObject(annId) ?: return@forEach
            kinds.keys().forEach { kind ->
                val k = key(annId, kind)
                totals[k] = kinds.optInt(kind) + (pending[k] ?: 0)
            }
        }
    }

    private fun paint() {
        counters.forEach { (k, views) ->
            val text = (totals[k] ?: 0).toString()
            views.forEach { it.text = text }
        }
    }

    // ---------- 动画 ----------

    /** 动画都画在 content 这层 FrameLayout 上，盖住页面又不影响下面的滚动。 */
    private fun layer(): ViewGroup? = activity.findViewById(android.R.id.content)

    /** 控件中心在 content 坐标系里的位置，回 null 表示还没上屏、不该动画。 */
    private fun centerIn(host: ViewGroup, view: View): Pair<Float, Float>? {
        val hostAt = IntArray(2).also { host.getLocationOnScreen(it) }
        val viewAt = IntArray(2).also { view.getLocationOnScreen(it) }
        if (view.width == 0 || view.height == 0) return null
        return (viewAt[0] - hostAt[0] + view.width / 2f) to (viewAt[1] - hostAt[1] + view.height / 2f)
    }

    private fun floatingText(value: String, size: Float) = TextView(activity).apply {
        text = value
        textSize = size
        includeFontPadding = false
        layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        )
    }

    /**
     * 表情按钮：从按钮飞向屏幕中间偏上，一路放大 + 乱转，落点砸一下再化开。
     * 位置用 translationX/Y 而不是 layout 参数，省得每帧触发一次布局。
     */
    private fun fly(from: View, emoji: String) {
        val host = layer() ?: return
        if (!animate) return
        val (x0, y0) = centerIn(host, from) ?: return
        val x1 = host.width / 2f + (Random.nextFloat() - .5f) * host.width * .5f
        val y1 = host.height * .42f + (Random.nextFloat() - .5f) * host.height * .24f
        val spin = (Random.nextFloat() - .5f) * 900f

        val view = floatingText(emoji, THROWN_SP)
        host.addView(view)
        view.post {
            // 拿到实际尺寸才能把它摆成以 (x,y) 为中心
            val halfW = view.width / 2f
            val halfH = view.height / 2f
            // 中途抬高一点走出弧线，和网页端那条 cubic-bezier 的观感对齐
            val midX = (x0 + x1) / 2f
            val midY = minOf(y0, y1) - dp(40)
            view.scaleX = .35f; view.scaleY = .35f

            val path = ValueAnimator.ofFloat(0f, 1f).apply {
                duration = FLY_MS
                interpolator = DecelerateInterpolator(1.6f)
                addUpdateListener { anim ->
                    val t = anim.animatedValue as Float
                    // 二次贝塞尔：起点 → 中途高点 → 落点
                    val inv = 1 - t
                    val x = inv * inv * x0 + 2 * inv * t * midX + t * t * x1
                    val y = inv * inv * y0 + 2 * inv * t * midY + t * t * y1
                    view.translationX = x - halfW
                    view.translationY = y - halfH
                    val scale = .35f + (2.6f - .35f) * t
                    view.scaleX = scale; view.scaleY = scale
                    view.rotation = spin * t
                }
            }
            path.addListener(onEnd {
                host.removeView(view)
                splat(host, emoji, x1, y1)
            })
            path.start()
        }
    }

    /** 落点效果：一圈扩散的环 + 砸扁的表情。 */
    private fun splat(host: ViewGroup, emoji: String, x: Float, y: Float) {
        val ring = View(activity).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setStroke(dp(3), color(R.color.text_muted))
            }
            layoutParams = FrameLayout.LayoutParams(dp(70), dp(70))
            translationX = x - dp(35)
            translationY = y - dp(35)
            scaleX = .3f; scaleY = .3f; alpha = .55f
        }
        host.addView(ring)
        ring.animate().scaleX(1.6f).scaleY(1.6f).alpha(0f)
            .setDuration(RING_MS).setInterpolator(DecelerateInterpolator())
            .withEndAction { host.removeView(ring) }
            .start()

        val mark = floatingText(emoji, THROWN_SP)
        host.addView(mark)
        mark.post {
            mark.translationX = x - mark.width / 2f
            mark.translationY = y - mark.height / 2f
            // 砸扁 → 回弹 → 化开
            val squash = AnimatorSet().apply {
                playTogether(
                    ObjectAnimator.ofFloat(mark, View.SCALE_X, 3.4f, 2.8f, 3.0f),
                    ObjectAnimator.ofFloat(mark, View.SCALE_Y, 2.1f, 2.9f, 2.8f),
                    ObjectAnimator.ofFloat(mark, View.ROTATION, 0f, 6f, -4f),
                    ObjectAnimator.ofFloat(mark, View.ALPHA, 1f, .95f, 0f),
                )
                duration = SPLAT_MS
                interpolator = DecelerateInterpolator()
            }
            squash.addListener(onEnd { host.removeView(mark) })
            squash.start()
        }
    }

    /** 应援按钮：几个 emoji 从按钮上方一起冒出来，边升边放大，淡入再淡出。 */
    private fun cheer(from: View, emojis: String) {
        val host = layer() ?: return
        if (!animate) return
        val parts = splitEmojis(emojis)
        if (parts.isEmpty()) return
        val (cx, cy) = centerIn(host, from) ?: return
        val top = cy - from.height / 2f

        parts.forEachIndexed { index, emoji ->
            val dx = (index - (parts.size - 1) / 2f) * dp(44) + (Random.nextFloat() - .5f) * dp(12)
            val rise = dp(70) + Random.nextFloat() * dp(30)
            val view = floatingText(emoji, THROWN_SP)
            host.addView(view)
            view.post {
                val halfW = view.width / 2f
                val halfH = view.height / 2f
                view.translationX = cx + dx - halfW
                view.alpha = 0f
                val lift = ValueAnimator.ofFloat(0f, 1f).apply {
                    duration = CHEER_MS
                    interpolator = DecelerateInterpolator()
                    addUpdateListener { anim ->
                        val t = anim.animatedValue as Float
                        view.translationY = top - rise * t - halfH
                        // 0 → .35 淡入并涨到 1.25，之后继续涨到 1.5 并淡出
                        val scale = if (t < .35f) .5f + (1.25f - .5f) * (t / .35f)
                                    else 1.25f + (1.5f - 1.25f) * ((t - .35f) / .65f)
                        view.scaleX = scale; view.scaleY = scale
                        view.alpha = if (t < .35f) t / .35f else 1f - (t - .35f) / .65f
                    }
                }
                lift.addListener(onEnd { host.removeView(view) })
                lift.start()
            }
        }
    }

    // ---------- 小工具 ----------

    private fun onEnd(action: () -> Unit) = object : AnimatorListenerAdapter() {
        override fun onAnimationEnd(animation: Animator) = action()
    }

    /** 一排按钮，窄屏放不下就换行。 */
    private fun flowRow() = FlowLayout(activity).apply {
        gap = dp(8)
        setPadding(0, dp(10), 0, 0)
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
        )
    }

    private fun color(id: Int) = ContextCompat.getColor(activity, id)
    private fun dp(value: Int) = (value * activity.resources.displayMetrics.density).toInt()
    private fun dp(value: Float) = value * activity.resources.displayMetrics.density

    companion object {
        /** 应援按钮的 kind，与后端 REACTION_CHEER_KIND 同值。 */
        private const val CHEER_KIND = "cheer"

        /** 连点攒够这么久才发一包，与网页端同值。 */
        private const val FLUSH_DELAY_MS = 700L

        /** 一次最多认 20 下，与后端 REACTION_MAX_PER_POST 同值，多的后端也会削掉。 */
        private const val MAX_PER_POST = 20

        private const val THROWN_SP = 26f
        private const val FLY_MS = 460L
        private const val RING_MS = 420L
        private const val SPLAT_MS = 700L
        private const val CHEER_MS = 1100L

        /** 把一串 emoji 按字素拆开（🔥 一个码点，👨‍👩‍👧 一串码点，都算一个）。 */
        fun splitEmojis(text: String): List<String> {
            val out = mutableListOf<String>()
            val it = BreakIterator.getCharacterInstance()
            it.setText(text)
            var start = it.first()
            var end = it.next()
            while (end != BreakIterator.DONE) {
                text.substring(start, end).takeIf { part -> part.isNotBlank() }?.let(out::add)
                start = end
                end = it.next()
            }
            return out
        }

        private fun jsonStrings(array: JSONArray?): List<String> =
            (0 until (array?.length() ?: 0)).mapNotNull { array?.optString(it)?.takeIf(String::isNotBlank) }
    }
}

/**
 * 只为互动条写的换行流式布局：一排放不下就折到下一行。
 * 这个项目没引 flexbox 依赖，为了几个按钮也犯不上加一个。
 */
class FlowLayout(context: android.content.Context) : ViewGroup(context) {

    var gap: Int = 0

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val limit = MeasureSpec.getSize(widthMeasureSpec) - paddingLeft - paddingRight
        var x = 0
        var rowHeight = 0
        var height = 0
        children().forEach { child ->
            measureChild(child, widthMeasureSpec, heightMeasureSpec)
            if (x > 0 && x + child.measuredWidth > limit) {
                height += rowHeight + gap
                x = 0
                rowHeight = 0
            }
            x += child.measuredWidth + gap
            rowHeight = maxOf(rowHeight, child.measuredHeight)
        }
        height += rowHeight
        setMeasuredDimension(
            MeasureSpec.getSize(widthMeasureSpec),
            height + paddingTop + paddingBottom,
        )
    }

    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {
        val limit = r - l - paddingLeft - paddingRight
        var x = paddingLeft
        var y = paddingTop
        var rowHeight = 0
        children().forEach { child ->
            if (x > paddingLeft && x + child.measuredWidth > limit) {
                y += rowHeight + gap
                x = paddingLeft
                rowHeight = 0
            }
            child.layout(x, y, x + child.measuredWidth, y + child.measuredHeight)
            x += child.measuredWidth + gap
            rowHeight = maxOf(rowHeight, child.measuredHeight)
        }
    }

    private fun children() = (0 until childCount).map { getChildAt(it) }.filter { it.visibility != GONE }
}
