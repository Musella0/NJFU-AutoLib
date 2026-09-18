package com.autolib.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import androidx.core.content.ContextCompat

/**
 * 学习热力图：每列一周（周一在最上），从左到右由远及近，
 * 与网页端 `heatmapHtml()` 画的是同一张图、同一套配色分档。
 *
 * 宽度会超出屏幕，使用方需要把它放进 HorizontalScrollView。
 */
class HeatmapView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    /** [filler] 是为了补满最后一列的占位格，不绘制。 */
    data class Cell(
        val date: String,
        val minutes: Int,
        val visits: Int,
        val isToday: Boolean,
        val filler: Boolean = false,
    )

    /** 按列优先排列：每 7 个构成一列。 */
    var cells: List<Cell> = emptyList()
        set(value) {
            field = value
            requestLayout()
            invalidate()
        }

    /** 每列列首所属月份，null 表示该列不标注。 */
    var monthLabels: List<String?> = emptyList()
        set(value) {
            field = value
            invalidate()
        }

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val outline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
    }
    private val monthPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(9f)
        typeface = Typeface.create("sans-serif", Typeface.NORMAL)
    }
    private val rect = RectF()

    private val cellSize = dp(12f)
    private val gap = dp(3f)
    private val radius = dp(2.5f)
    private val monthRowHeight = dp(15f)

    private val columns: Int get() = (cells.size + 6) / 7

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = paddingLeft + paddingRight + columns * (cellSize + gap)
        val height = paddingTop + paddingBottom + monthRowHeight + ROWS * (cellSize + gap)
        setMeasuredDimension(
            resolveSize(width.toInt(), widthMeasureSpec),
            resolveSize(height.toInt(), heightMeasureSpec),
        )
    }

    override fun onDraw(canvas: Canvas) {
        if (cells.isEmpty()) return
        val gridTop = paddingTop + monthRowHeight

        cells.forEachIndexed { index, cell ->
            if (cell.filler) return@forEachIndexed
            val left = paddingLeft + (index / ROWS) * (cellSize + gap)
            val top = gridTop + (index % ROWS) * (cellSize + gap)
            rect.set(left, top, left + cellSize, top + cellSize)
            fill.color = color(heatColor(cell.minutes))
            canvas.drawRoundRect(rect, radius, radius, fill)
            if (cell.isToday) {
                outline.color = color(R.color.primary)
                canvas.drawRoundRect(rect, radius, radius, outline)
            }
        }

        monthPaint.color = color(R.color.text_muted)
        monthLabels.forEachIndexed { column, label ->
            if (label == null) return@forEachIndexed
            canvas.drawText(
                label,
                paddingLeft + column * (cellSize + gap),
                paddingTop + monthPaint.textSize,
                monthPaint,
            )
        }
    }

    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Float) = value * resources.displayMetrics.density
    private fun sp(value: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value, resources.displayMetrics)

    companion object {
        private const val ROWS = 7

        /** 每天的学习时长分 5 档，阈值与网页端 heatLevel() 保持一致。 */
        fun heatColor(minutes: Int) = when {
            minutes <= 0 -> R.color.heat0
            minutes <= 120 -> R.color.heat1
            minutes <= 240 -> R.color.heat2
            minutes <= 360 -> R.color.heat3
            else -> R.color.heat4
        }
    }
}
