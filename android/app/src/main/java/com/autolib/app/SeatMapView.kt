package com.autolib.app

import android.annotation.SuppressLint
import android.content.Context
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Typeface
import android.util.AttributeSet
import android.util.TypedValue
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import androidx.core.content.ContextCompat
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * 按图选座，对应网页端的 `.seatmap`：拖动平移、双指捏合（或双击 / 滚轮）缩放，
 * 点图上任意位置选中离它最近的座位。
 *
 * 座位密的区域一屏里挤着四百多个点，指头盖住的范围比点大得多，所以不做「点中圆点」
 * 判定，一律取最近的那个——和网页端 `pickNearestSeat()` 是同一套算法和同一个容差。
 */
class SeatMapView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    /** 选中的座位变了（含切换区域后被清空）。 */
    var onPicked: ((String?) -> Unit)? = null

    /** 我的座位优先级，按顺序——图上标序号。 */
    var chosen: List<String> = emptyList()
        set(value) {
            field = value
            invalidate()
        }

    /** 座位号 → 除我以外把它放进优先级的人数。 */
    var heat: Map<String, Int> = emptyMap()
        set(value) {
            field = value
            invalidate()
        }

    var picked: String? = null
        private set

    /** 当前显示的区域号，异步回来的底图靠它认领自己该不该画。 */
    val roomId: String? get() = area?.roomId

    private var area: SeatLayout.Area? = null
    private var bitmap: Bitmap? = null
    private var ratio = DEFAULT_RATIO
    private var scale = 1f
    private var offsetX = 0f
    private var offsetY = 0f

    /** 底图还没到位时显示的说明。 */
    var placeholder: String = "底图加载中…"
        set(value) {
            field = value
            invalidate()
        }

    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val borderPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2f)
    }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        textAlign = Paint.Align.CENTER
        typeface = Typeface.create("sans-serif-monospace", Typeface.BOLD)
    }
    private val ordinalPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(9f)
        textAlign = Paint.Align.CENTER
        typeface = Typeface.create("sans-serif", Typeface.BOLD)
    }
    private val hintPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(13f)
        textAlign = Paint.Align.CENTER
    }
    private val imagePaint = Paint(Paint.FILTER_BITMAP_FLAG).apply {
        // 平面图是白底黑线的施工图，深色模式下反色，免得半夜刺眼。
        // -0.76v + 224 等价于网页端的 filter:invert(.88)：白纸变成深灰而不是纯黑。
        if (isNightMode()) colorFilter = ColorMatrixColorFilter(
            ColorMatrix(
                floatArrayOf(
                    -0.76f, 0f, 0f, 0f, 224f,
                    0f, -0.76f, 0f, 0f, 224f,
                    0f, 0f, -0.76f, 0f, 224f,
                    0f, 0f, 0f, 1f, 0f,
                )
            )
        )
    }

    private val destination = RectF()
    private val tagRect = RectF()
    private val clipPath = Path()
    private val radius = dp(12f)

    private val scaleDetector = ScaleGestureDetector(
        context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                zoomBy(detector.scaleFactor, detector.focusX, detector.focusY)
                return true
            }
        },
    )

    private val gestureDetector = GestureDetector(
        context,
        object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(event: MotionEvent) = true

            override fun onScroll(
                start: MotionEvent?,
                current: MotionEvent,
                distanceX: Float,
                distanceY: Float,
            ): Boolean {
                if (scaleDetector.isInProgress) return false
                offsetX -= distanceX
                offsetY -= distanceY
                clamp()
                invalidate()
                return true
            }

            override fun onSingleTapUp(event: MotionEvent): Boolean {
                pickNearest(event.x, event.y)
                return true
            }

            /** 模拟器和没有多点触控的设备捏合不了，双击给一档放大。 */
            override fun onDoubleTap(event: MotionEvent): Boolean {
                val target = if (scale < DOUBLE_TAP_SCALE - 0.01f) DOUBLE_TAP_SCALE else fitScale()
                zoomBy(target / scale, event.x, event.y)
                return true
            }
        },
    )

    /** 换区域：底图和座位一起替换，视图复位。选中的座位不在新区域里就清空。 */
    fun show(area: SeatLayout.Area, bitmap: Bitmap?) {
        this.area = area
        this.bitmap = bitmap
        ratio = if (bitmap != null && bitmap.width > 0) {
            bitmap.height.toFloat() / bitmap.width
        } else {
            DEFAULT_RATIO
        }
        if (picked != null && area.seats.none { it.name == picked }) {
            picked = null
            onPicked?.invoke(null)
        }
        reset()
    }

    /** 预选一个座位并把视图挪过去。 */
    fun select(name: String) {
        picked = name
        if (width > 0) centerOn(name)
        invalidate()
        onPicked?.invoke(name)
    }

    /** 复位缩放和位置；有选中座位就把它挪到中间。 */
    fun reset() {
        if (width == 0 || height == 0) return
        scale = fitScale()
        offsetX = 0f
        offsetY = 0f
        val current = picked
        if (current != null) centerOn(current) else clamp()
        invalidate()
    }

    /**
     * 图宽等于容器宽时算 1 倍；竖屏下整张图缩得太小，所以按容器高度补一档，
     * 与网页端 `resetSeatMapView()` 同样封顶在 2.4 倍。
     */
    private fun fitScale(): Float {
        if (width == 0 || height == 0) return 1f
        return max(MIN_SCALE, min(height / (width * ratio), MAX_FIT_SCALE))
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        reset()
    }

    override fun onDraw(canvas: Canvas) {
        clipPath.reset()
        clipPath.addRoundRect(
            0f, 0f, width.toFloat(), height.toFloat(), radius, radius, Path.Direction.CW,
        )
        val saved = canvas.save()
        canvas.clipPath(clipPath)
        canvas.drawColor(color(R.color.surface_alt))

        val map = area
        val image = bitmap
        if (map == null || image == null) {
            hintPaint.color = color(R.color.text_muted)
            canvas.drawText(placeholder, width / 2f, height / 2f, hintPaint)
        } else {
            drawMap(canvas, map, image)
        }

        canvas.restoreToCount(saved)
        borderPaint.color = color(R.color.stroke)
        val inset = borderPaint.strokeWidth / 2f
        canvas.drawRoundRect(
            inset, inset, width - inset, height - inset, radius, radius, borderPaint,
        )
    }

    private fun drawMap(canvas: Canvas, map: SeatLayout.Area, image: Bitmap) {
        val imageW = width * scale
        val imageH = imageW * ratio
        destination.set(offsetX, offsetY, offsetX + imageW, offsetY + imageH)
        canvas.drawBitmap(image, null, destination, imagePaint)

        val dot = dotSize()
        val current = picked
        map.seats.forEach { seat ->
            if (seat.name == current) return@forEach
            val cx = offsetX + imageW * seat.x / 100f + dot / 2f
            val cy = offsetY + imageH * seat.y / 100f + dot / 2f
            if (cx < -dot || cy < -dot || cx > width + dot || cy > height + dot) return@forEach
            val ordinal = chosen.indexOf(seat.name)
            dotPaint.color = color(seatColor(seat.name, ordinal))
            canvas.drawCircle(cx, cy, dot / 2f, dotPaint)
            if (ordinal < 0) return@forEach
            // 圆点里塞不下字，序号浮在点上方
            ordinalPaint.color = color(R.color.success)
            canvas.drawText("${ordinal + 1}", cx, cy - dot / 2f - dp(2f), ordinalPaint)
        }
        if (current != null) drawPickedSeat(canvas, map, current, imageW, imageH, dot)
    }

    /** 选中的点大一圈、带光晕，并在下方挂一个座位号标签。 */
    private fun drawPickedSeat(
        canvas: Canvas,
        map: SeatLayout.Area,
        name: String,
        imageW: Float,
        imageH: Float,
        dot: Float,
    ) {
        val seat = map.seats.firstOrNull { it.name == name } ?: return
        val cx = offsetX + imageW * seat.x / 100f + dot / 2f
        val cy = offsetY + imageH * seat.y / 100f + dot / 2f
        val outer = max(dot, dp(9f)) / 2f

        ringPaint.color = color(R.color.accent_soft)
        ringPaint.strokeWidth = dp(3f)
        canvas.drawCircle(cx, cy, outer + dp(2f), ringPaint)
        dotPaint.color = color(R.color.primary)
        canvas.drawCircle(cx, cy, outer, dotPaint)
        ringPaint.color = color(R.color.stroke)
        ringPaint.strokeWidth = dp(1.5f)
        canvas.drawCircle(cx, cy, outer, ringPaint)

        val padX = dp(5f)
        val padY = dp(3f)
        val textWidth = labelPaint.measureText(name)
        val metrics = labelPaint.fontMetrics
        val top = cy + outer + dp(6f)
        tagRect.set(
            cx - textWidth / 2f - padX,
            top,
            cx + textWidth / 2f + padX,
            top + (metrics.bottom - metrics.top) + padY * 2,
        )
        dotPaint.color = color(R.color.surface)
        canvas.drawRoundRect(tagRect, dp(5f), dp(5f), dotPaint)
        ringPaint.color = color(R.color.stroke)
        ringPaint.strokeWidth = dp(1.5f)
        canvas.drawRoundRect(tagRect, dp(5f), dp(5f), ringPaint)
        labelPaint.color = color(R.color.text_primary)
        canvas.drawText(name, cx, tagRect.top + padY - metrics.top, labelPaint)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        // 外层是 ScrollView，不拦住的话纵向拖动会被它抢去滚页面
        if (event.actionMasked == MotionEvent.ACTION_DOWN) {
            parent?.requestDisallowInterceptTouchEvent(true)
        }
        scaleDetector.onTouchEvent(event)
        gestureDetector.onTouchEvent(event)
        if (event.actionMasked == MotionEvent.ACTION_UP ||
            event.actionMasked == MotionEvent.ACTION_CANCEL
        ) {
            parent?.requestDisallowInterceptTouchEvent(false)
        }
        return true
    }

    /** 鼠标滚轮缩放，给模拟器和接了鼠标的平板用，对应网页端的 onSeatMapWheel。 */
    override fun onGenericMotionEvent(event: MotionEvent): Boolean {
        if (event.actionMasked == MotionEvent.ACTION_SCROLL) {
            val vertical = event.getAxisValue(MotionEvent.AXIS_VSCROLL)
            if (vertical != 0f) {
                zoomBy(if (vertical > 0) WHEEL_STEP else 1f / WHEEL_STEP, event.x, event.y)
                return true
            }
        }
        return super.onGenericMotionEvent(event)
    }

    private fun zoomBy(factor: Float, focusX: Float, focusY: Float) {
        val previous = scale
        scale = (scale * factor).coerceIn(MIN_SCALE, MAX_SCALE)
        val applied = scale / previous
        // 以手指（或指针）为锚点缩放，图上被按住的那个点保持不动
        offsetX = focusX - (focusX - offsetX) * applied
        offsetY = focusY - (focusY - offsetY) * applied
        clamp()
        invalidate()
    }

    /** 图比容器小就居中，比容器大就不许拖出边界。 */
    private fun clamp() {
        val imageW = width * scale
        val imageH = imageW * ratio
        offsetX = if (imageW <= width) (width - imageW) / 2f
        else offsetX.coerceIn(width - imageW, 0f)
        offsetY = if (imageH <= height) (height - imageH) / 2f
        else offsetY.coerceIn(height - imageH, 0f)
    }

    private fun centerOn(name: String) {
        val seat = area?.seats?.firstOrNull { it.name == name } ?: return
        val imageW = width * scale
        val dot = dotSize()
        offsetX = width / 2f - imageW * seat.x / 100f - dot / 2f
        offsetY = height / 2f - imageW * ratio * seat.y / 100f - dot / 2f
        clamp()
    }

    private fun pickNearest(px: Float, py: Float) {
        val map = area ?: return
        if (width == 0 || bitmap == null) return
        val imageW = width * scale
        val imageH = imageW * ratio
        val x = (px - offsetX) / imageW * 100f
        val y = (py - offsetY) / imageH * 100f
        // 坐标记的是圆点左上角，补半个点才是学生眼里那个座位的位置
        val offX = dotSize() / 2f / imageW * 100f
        val offY = dotSize() / 2f / imageH * 100f

        var best: SeatLayout.Seat? = null
        var bestDistance = Float.MAX_VALUE
        map.seats.forEach { seat ->
            val dx = seat.x + offX - x
            // y 按底图长宽比折算回等比距离，否则横向的座位总显得更近
            val dy = (seat.y + offY - y) * ratio
            val distance = dx * dx + dy * dy
            if (distance < bestDistance) {
                bestDistance = distance
                best = seat
            }
        }
        val target = best ?: return
        if (sqrt(bestDistance) > TAP_TOLERANCE) return   // 离所有座位都太远，当成误触
        picked = target.name
        invalidate()
        onPicked?.invoke(picked)
    }

    /** 圆点跟着图一起放大，和网页端一样；缩到最小时留个下限，免得看不见。 */
    private fun dotSize() = max(dp(2.5f), dp(DOT_DP) * scale)

    private fun seatColor(name: String, ordinal: Int): Int {
        val crowd = heat[name] ?: 0
        return when {
            ordinal >= 0 -> R.color.success        // 我的优先级里已经有它
            crowd >= 3 -> R.color.danger           // 一堆人盯着，抢不到的概率高
            crowd > 0 -> R.color.warn
            else -> R.color.stroke_muted
        }
    }

    private fun isNightMode() =
        (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES

    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Float) = value * resources.displayMetrics.density
    private fun sp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value, resources.displayMetrics)

    companion object {
        /** 底图都是 1600×900，取不到 bitmap 时按这个比例先撑住布局。 */
        private const val DEFAULT_RATIO = 0.5625f
        /**
         * 圆点直径。网页端是 6 CSS px，但手机屏窄、座位密的区域（二层 A 区 441 座）
         * 照搬会糊成一片，收到 4dp 才看得出一个个座位。
         */
        private const val DOT_DP = 4f
        private const val MIN_SCALE = 1f
        private const val MAX_SCALE = 10f
        private const val MAX_FIT_SCALE = 2.4f
        private const val DOUBLE_TAP_SCALE = 4f
        private const val WHEEL_STEP = 1.25f

        /** 容差按「底图宽度的百分之几」算，和网页端同值。 */
        private const val TAP_TOLERANCE = 8f
    }
}
