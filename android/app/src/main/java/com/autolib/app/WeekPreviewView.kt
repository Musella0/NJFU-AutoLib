package com.autolib.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import androidx.core.content.ContextCompat

/**
 * 主页「本周配置预览」。把每天配置的预约时段画成 08:00–22:00 轨道上的色条，
 * 对应网页端 `renderWeekPreview()` 的条形图，包括顶部小时刻度、今/明标签，
 * 以及单段时在色条上方标出的起止时间。
 */
class WeekPreviewView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    enum class Tone { TODAY, TOMORROW, ACTIVE, OFF }

    /** 一天的预览数据。[segments] 是小时值区间，例如 08:30 记作 8.5f。 */
    data class Day(
        val label: String,
        val tag: String?,
        val segments: List<Pair<Float, Float>>,
        val tone: Tone,
        val showTimeLabel: Boolean,
        /** 标签配色可以和色条不同：今天即使休息，星期名也保持高亮。 */
        val labelTone: Tone = tone,
    )

    var days: List<Day> = emptyList()
        set(value) {
            field = value
            requestLayout()
            invalidate()
        }

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val outline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
    }
    private val tickPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(10f)
        typeface = Typeface.create("sans-serif-monospace", Typeface.BOLD)
    }
    private val tickMark = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeWidth = dp(1f) }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = sp(12f) }
    private val tagPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(9f)
        typeface = Typeface.create("sans-serif", Typeface.BOLD)
        textAlign = Paint.Align.CENTER
    }
    private val timePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(9f)
        typeface = Typeface.create("sans-serif-monospace", Typeface.NORMAL)
        textAlign = Paint.Align.RIGHT
    }
    private val rect = RectF()
    private val dash = DashPathEffect(floatArrayOf(dp(4f), dp(3f)), 0f)

    private val labelWidth = dp(62f)
    private val trackGap = dp(8f)
    private val tickRowHeight = dp(22f)
    private val rowHeight = dp(30f)
    private val barHeight = dp(13f)
    private val barRadius = dp(4f)

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = resolveSize(suggestedMinimumWidth, widthMeasureSpec)
        val height = paddingTop + paddingBottom + tickRowHeight + rowHeight * days.size
        setMeasuredDimension(width, resolveSize(height.toInt(), heightMeasureSpec))
    }

    override fun onDraw(canvas: Canvas) {
        if (days.isEmpty()) return
        val trackLeft = paddingLeft + labelWidth + trackGap
        val trackRight = (width - paddingRight).toFloat()
        if (trackRight <= trackLeft) return

        drawTicks(canvas, trackLeft, trackRight)

        var top = paddingTop + tickRowHeight
        days.forEach { day ->
            drawLabel(canvas, day, top)
            val barTop = top + (rowHeight - barHeight) / 2f + dp(3f)
            if (day.segments.isEmpty()) {
                drawBar(canvas, trackLeft, trackRight, barTop, Tone.OFF)
            } else {
                day.segments.forEach { (from, to) ->
                    val left = trackLeft + fraction(from) * (trackRight - trackLeft)
                    val right = trackLeft + fraction(to) * (trackRight - trackLeft)
                    drawBar(canvas, left, maxOf(right, left + dp(4f)), barTop, day.tone)
                    if (day.showTimeLabel && day.segments.size == 1) {
                        timePaint.color = color(if (day.tone == Tone.TOMORROW) R.color.tomorrow else R.color.primary)
                        canvas.drawText(
                            "${format(from)}-${format(to)}",
                            maxOf(right, left + dp(4f)),
                            barTop - dp(2f),
                            timePaint,
                        )
                    }
                }
            }
            top += rowHeight
        }
    }

    private fun drawTicks(canvas: Canvas, trackLeft: Float, trackRight: Float) {
        tickPaint.color = color(R.color.text_secondary)
        tickMark.color = color(R.color.text_muted)
        val baseline = paddingTop + sp(10f)
        HOUR_TICKS.forEach { hour ->
            val x = trackLeft + fraction(hour) * (trackRight - trackLeft)
            tickPaint.textAlign = when (hour) {
                HOUR_MIN -> Paint.Align.LEFT
                HOUR_MAX -> Paint.Align.RIGHT
                else -> Paint.Align.CENTER
            }
            canvas.drawText(hour.toInt().toString(), x, baseline, tickPaint)
            canvas.drawLine(x, baseline + dp(3f), x, baseline + dp(8f), tickMark)
        }
    }

    private fun drawLabel(canvas: Canvas, day: Day, top: Float) {
        val left = paddingLeft.toFloat()
        val centerY = top + rowHeight / 2f + dp(3f)
        val tagWidth = dp(17f)
        val tagGap = dp(4f)
        // 标签位固定预留，没有今/明标签的行也从同一处起排，七行才对得齐
        day.tag?.let { tag ->
            val tagHeight = dp(14f)
            rect.set(left, centerY - tagHeight + dp(3f), left + tagWidth, centerY + dp(3f))
            fill.color = color(if (tag == "明") R.color.tomorrow else R.color.primary)
            canvas.drawRoundRect(rect, dp(3f), dp(3f), fill)
            tagPaint.color = color(R.color.on_primary)
            canvas.drawText(tag, rect.centerX(), centerY, tagPaint)
        }
        val x = left + tagWidth + tagGap
        labelPaint.color = when (day.labelTone) {
            Tone.TODAY -> color(R.color.primary)
            Tone.OFF -> color(R.color.stroke_muted)
            else -> color(R.color.text_secondary)
        }
        labelPaint.typeface = Typeface.create(
            "sans-serif",
            if (day.labelTone == Tone.TODAY) Typeface.BOLD else Typeface.NORMAL,
        )
        canvas.drawText(day.label, x, centerY, labelPaint)
    }

    private fun drawBar(canvas: Canvas, left: Float, right: Float, top: Float, tone: Tone) {
        rect.set(left, top, right, top + barHeight)
        val (fillColor, strokeColor) = when (tone) {
            Tone.TODAY -> R.color.primary to R.color.primary
            Tone.TOMORROW -> R.color.tomorrow_soft to R.color.tomorrow
            Tone.ACTIVE -> R.color.accent_soft to R.color.primary
            Tone.OFF -> R.color.surface to R.color.stroke_muted
        }
        fill.color = color(fillColor)
        canvas.drawRoundRect(rect, barRadius, barRadius, fill)
        outline.color = color(strokeColor)
        outline.pathEffect = if (tone == Tone.TOMORROW || tone == Tone.OFF) dash else null
        canvas.drawRoundRect(rect, barRadius, barRadius, outline)
    }

    private fun fraction(hour: Float) = ((hour - HOUR_MIN) / (HOUR_MAX - HOUR_MIN)).coerceIn(0f, 1f)
    private fun format(hour: Float): String {
        val minutes = Math.round(hour * 60)
        return "%02d:%02d".format(minutes / 60, minutes % 60)
    }
    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Float) = value * resources.displayMetrics.density
    private fun sp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value, resources.displayMetrics)

    private companion object {
        const val HOUR_MIN = 8f
        const val HOUR_MAX = 22f
        val HOUR_TICKS = listOf(8f, 12f, 16f, 20f, 22f)
    }
}
