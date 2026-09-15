package com.autolib.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Typeface
import android.util.AttributeSet
import android.util.TypedValue
import android.view.MotionEvent
import android.view.View
import androidx.core.content.ContextCompat

/**
 * 通用的小折线图，与网页端 `drawLineChart()` 画的是同一张图：
 * 纵轴是百分比刻度，横轴按 [xEvery] 抽样标注；每条线可以带面积填充，
 * 值为 null 的点断开；只给最后一个点画圆点。
 *
 * 手指按着在图上滑动显示十字线和提示框，抬起时把那个点选中并回调 [onSelect]；
 * [compact] 是设置页卡片上的迷你版：不画轴、不响应触摸。
 */
class LineChartView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    /** [color] 是颜色资源 id；[values] 里的 null 表示那一点没数据。 */
    data class Series(val name: String, val color: Int, val values: List<Float?>, val area: Boolean = false)

    var labels: List<String> = emptyList()
    var series: List<Series> = emptyList()
    var xEvery = 0
    var compact = false
    /** 非 compact 时的高度（dp），与网页端 height 参数对应。 */
    var heightDp = 160
    /** 提示框标题与附加文字，按点的下标给。 */
    var tooltipTitle: ((Int) -> String)? = null
    var tooltipExtra: ((Int) -> String)? = null
    var onSelect: ((Int) -> Unit)? = null

    /** 抬手后钉住的点；-1 表示没有。 */
    var selected = -1
        set(value) { field = value; invalidate() }
    /** 手指按着时跟随的点。 */
    private var hover = -1

    fun bind(labels: List<String>, series: List<Series>, selected: Int = -1) {
        this.labels = labels
        this.series = series
        this.selected = selected
        hover = -1
        invalidate()
    }

    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = dp(2f)
        strokeJoin = Paint.Join.ROUND; strokeCap = Paint.Cap.ROUND
    }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = dp(1f) }
    private val tickPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = sp(9f) }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val dotRing = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = dp(2f) }
    private val tipBg = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val tipStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = dp(1f) }
    private val tipTitle = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f); typeface = Typeface.create("sans-serif", Typeface.BOLD)
    }
    private val tipText = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = sp(11f) }
    private val path = Path()
    private val rect = RectF()

    private val padL get() = if (compact) dp(4f) else dp(34f)
    private val padR get() = if (compact) dp(4f) else dp(12f)
    private val padT get() = if (compact) dp(4f) else dp(12f)
    private val padB get() = if (compact) dp(4f) else dp(22f)

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val height = if (compact) dp(40f) else dp(heightDp.toFloat())
        setMeasuredDimension(
            resolveSize(suggestedMinimumWidth, widthMeasureSpec),
            resolveSize(height.toInt(), heightMeasureSpec),
        )
    }

    // ---- 坐标 ----

    private val n get() = labels.size
    private val plotW get() = width - padL - padR
    private val plotH get() = height - padT - padB

    /** 纵轴顶到刻度的整数倍，至少 10%，别让一条 18% 的线贴着天花板。 */
    private fun yScale(): Pair<Float, Float> {
        val all = series.flatMap { it.values }.filterNotNull()
        val rawMax = all.maxOrNull() ?: 0f
        val step = if (rawMax <= 30f) 5f else if (rawMax <= 60f) 10f else 20f
        val max = maxOf(10f, Math.ceil((rawMax / step).toDouble()).toFloat() * step)
        return step to max
    }

    private fun x(i: Int) = if (n > 1) padL + i.toFloat() / (n - 1) * plotW else padL + plotW / 2
    private fun y(v: Float, yMax: Float) = padT + plotH - v / yMax * plotH

    override fun onDraw(canvas: Canvas) {
        if (n == 0 || series.isEmpty()) return
        val (yStep, yMax) = yScale()

        if (!compact) {
            gridPaint.color = color(R.color.stroke_muted)
            tickPaint.color = color(R.color.text_muted)
            var v = 0f
            while (v <= yMax + 0.01f) {
                val yy = y(v, yMax)
                canvas.drawLine(padL, yy, width - padR, yy, gridPaint)
                tickPaint.textAlign = Paint.Align.RIGHT
                canvas.drawText("${v.toInt()}%", padL - dp(6f), yy + dp(3f), tickPaint)
                v += yStep
            }
            // 横轴标签：最多 6 个，或者按 xEvery 固定间隔
            val every = if (xEvery > 0) xEvery else maxOf(1, Math.ceil(n / 6.0).toInt())
            var i = 0
            while (i < n) {
                tickPaint.textAlign = when {
                    i == 0 -> Paint.Align.LEFT
                    i >= n - every -> Paint.Align.RIGHT
                    else -> Paint.Align.CENTER
                }
                canvas.drawText(labels[i], x(i), height - dp(6f), tickPaint)
                i += every
            }
        }

        val baseline = padT + plotH
        series.forEach { s ->
            val c = color(s.color)
            // 值为 null 处断开：每一段单独画线、单独填面积
            var segStart = -1
            fun flush(end: Int) {
                if (segStart < 0) return
                path.reset()
                for (i in segStart..end) {
                    val v = s.values[i] ?: continue
                    if (i == segStart) path.moveTo(x(i), y(v, yMax)) else path.lineTo(x(i), y(v, yMax))
                }
                linePaint.color = c
                canvas.drawPath(path, linePaint)
                if (s.area) {
                    path.lineTo(x(end), baseline); path.lineTo(x(segStart), baseline); path.close()
                    fillPaint.color = (c and 0x00FFFFFF) or 0x1A000000
                    canvas.drawPath(path, fillPaint)
                }
                segStart = -1
            }
            s.values.forEachIndexed { i, v ->
                if (v == null) flush(i - 1) else if (segStart < 0) segStart = i
            }
            flush(n - 1)
            // 只给最后一个有值的点画圆点
            val last = s.values.indexOfLast { it != null }
            if (last >= 0) drawDot(canvas, x(last), y(s.values[last]!!, yMax), c, dp(4f))
        }

        val focus = if (hover >= 0) hover else selected
        if (!compact && focus in 0 until n) drawFocus(canvas, focus, yMax, pinned = hover < 0)
    }

    private fun drawDot(canvas: Canvas, cx: Float, cy: Float, c: Int, r: Float) {
        dotPaint.color = c
        dotRing.color = color(R.color.surface)
        canvas.drawCircle(cx, cy, r, dotPaint)
        canvas.drawCircle(cx, cy, r, dotRing)
    }

    /** 十字线 + 各条线的圆点 + 提示框；提示框贴着十字线放，靠右时翻到左边。 */
    private fun drawFocus(canvas: Canvas, i: Int, yMax: Float, pinned: Boolean) {
        val fx = x(i)
        gridPaint.color = color(R.color.text_muted)
        canvas.drawLine(fx, padT, fx, padT + plotH, gridPaint)
        series.forEach { s ->
            val v = s.values[i] ?: return@forEach
            drawDot(canvas, fx, y(v, yMax), color(s.color), if (pinned) dp(5f) else dp(4f))
        }

        val title = tooltipTitle?.invoke(i) ?: labels[i]
        val rows = series.map { s ->
            val v = s.values[i]
            val value = if (v == null) "未拍" else String.format("%.1f%%", v)
            val extra = if (v != null) tooltipExtra?.invoke(i)?.let { " · $it" }.orEmpty() else ""
            Triple(color(s.color), value, s.name + extra)
        }
        tipTitle.color = color(R.color.text_primary)
        tipText.color = color(R.color.text_secondary)
        val pad = dp(8f)
        val lineH = tipText.textSize * 1.5f
        val key = dp(8f)
        val textW = maxOf(
            tipTitle.measureText(title),
            rows.maxOf { key + dp(6f) + tipTitle.measureText(it.second) + dp(6f) + tipText.measureText(it.third) },
        )
        val w = textW + pad * 2
        val h = pad * 2 + tipTitle.textSize * 1.3f + rows.size * lineH
        // 提示框太宽时别伸出图外
        val left = (if (fx > width * 0.6f) fx - dp(8f) - w else fx + dp(8f)).coerceIn(0f, maxOf(0f, width - w))
        val top = padT
        rect.set(left, top, left + w, top + h)
        tipBg.color = color(R.color.surface)
        tipStroke.color = color(R.color.stroke_muted)
        canvas.drawRoundRect(rect, dp(8f), dp(8f), tipBg)
        canvas.drawRoundRect(rect, dp(8f), dp(8f), tipStroke)
        var ty = top + pad + tipTitle.textSize
        tipTitle.textAlign = Paint.Align.LEFT
        canvas.drawText(title, left + pad, ty, tipTitle)
        rows.forEach { (c, value, name) ->
            ty += lineH
            dotPaint.color = c
            canvas.drawCircle(left + pad + key / 2, ty - tipText.textSize * 0.35f, key / 2, dotPaint)
            canvas.drawText(value, left + pad + key + dp(6f), ty, tipTitle)
            canvas.drawText(name, left + pad + key + dp(6f) + tipTitle.measureText(value) + dp(6f), ty, tipText)
        }
    }

    // ---- 触摸 ----

    private fun indexAt(px: Float): Int {
        if (n <= 1) return 0
        return Math.round((px - padL) / plotW * (n - 1)).coerceIn(0, n - 1)
    }

    private var downX = 0f
    private var downY = 0f
    private var horizontalDrag = false

    /**
     * 图在滚动页里：横着划是在图上找点，竖着划要让给父容器滚页面
     * （对应网页端的 touch-action: pan-y）。方向定下来之前不拦截父容器。
     */
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (compact || n == 0) return false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = event.x; downY = event.y; horizontalDrag = false
                hover = indexAt(event.x)
                invalidate()
            }
            MotionEvent.ACTION_MOVE -> {
                if (!horizontalDrag) {
                    val dx = Math.abs(event.x - downX)
                    val dy = Math.abs(event.y - downY)
                    if (dx > touchSlop && dx > dy) {
                        horizontalDrag = true
                        parent?.requestDisallowInterceptTouchEvent(true)
                    }
                }
                hover = indexAt(event.x)
                invalidate()
            }
            MotionEvent.ACTION_UP -> {
                val picked = indexAt(event.x)
                hover = -1
                if (onSelect != null) onSelect?.invoke(picked) else selected = picked
                invalidate()
                performClick()
            }
            MotionEvent.ACTION_CANCEL -> { hover = -1; invalidate() }
        }
        return true
    }

    private val touchSlop by lazy { android.view.ViewConfiguration.get(context).scaledTouchSlop.toFloat() }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Float) = value * resources.displayMetrics.density
    private fun sp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value, resources.displayMetrics)
}
