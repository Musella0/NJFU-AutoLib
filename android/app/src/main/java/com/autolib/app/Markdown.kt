package com.autolib.app

import android.graphics.Typeface
import android.text.Spannable
import android.text.SpannableStringBuilder
import android.text.style.BackgroundColorSpan
import android.text.style.LeadingMarginSpan
import android.text.style.QuoteSpan
import android.text.style.RelativeSizeSpan
import android.text.style.StrikethroughSpan
import android.text.style.StyleSpan
import android.text.style.TypefaceSpan
import android.text.style.URLSpan

/**
 * 极简 Markdown 渲染器，对应网页端的 `backend/static/markdown.js`。
 * 公告正文是同一份文本，两端得认同一套语法，否则同一条公告在手机上会露出满屏星号。
 *
 * 支持：# ~ ###### 标题 / **粗体** / *斜体* / ~~删除线~~ / `行内代码`
 *      ``` 代码块 / - * + 无序列表 / 1. 有序列表 / > 引用 / --- 分隔线
 *      [文字](链接) / 裸链接自动识别 / 换行
 *
 * 网页端输出 HTML，这里直接拼 Spannable：公告卡片和弹窗都是 TextView，
 * 中间过一道 Html.fromHtml 只会多一层转义上的坑。
 */
object Markdown {

    private val UL = Regex("""^\s*[-*+]\s+""")
    private val OL = Regex("""^\s*\d+[.)]\s+""")
    private val HR = Regex("""^\s*([-*_])\s*(\1\s*){2,}$""")
    private val HEADING = Regex("""^\s*(#{1,6})\s+(.*)$""")
    private val FENCE = Regex("""^\s*```""")
    private val QUOTE = Regex("""^\s*>\s?""")
    private val LINK = Regex("""^!?\[([^\]\n]*)\]\(([^)\s]+)\)""")
    private val CONTINUATION = Regex("""^\s{2,}\S""")
    private val BLOCK_START = Regex("""^\s*(#{1,6}\s|>|```)""")
    private val SAFE_URL = Regex("""^(https?://|mailto:)""", RegexOption.IGNORE_CASE)

    /**
     * 裸链接：域名允许中文（如 https://南林图书馆.中国/x.apk），
     * 路径之后只认 ASCII，免得把紧跟其后的中文正文一并吞掉。
     */
    private val BARE_URL = Regex(
        "^https?://[^\\s<>\"'*/?#，。；：！？、）】》「」…]+" +
            "([/?#][A-Za-z0-9\\-._~:/?#\\[\\]@!\$&'()+,;=%]*)?"
    )

    /** 句末标点不算链接的一部分，右括号另外按配对处理。 */
    private val URL_TAIL = Regex("""[.,:!?'"]+$""")

    /** 标题字号：`#` 起步就相当于网页端的 h3，别在卡片里太抢眼。 */
    private val HEADING_SCALE = mapOf(3 to 1.25f, 4 to 1.15f, 5 to 1.05f, 6 to 1.0f)

    private const val LIST_INDENT_PX = 36
    private const val QUOTE_STRIPE = 0x33808080
    private const val CODE_BACKGROUND = 0x1A808080

    /** 只放行 http(s) 和 mailto，挡掉 javascript: 之类。 */
    private fun safeUrl(url: String): String? =
        url.trim().takeIf { SAFE_URL.containsMatchIn(it) }

    fun render(source: String?): CharSequence {
        val lines = (source ?: "").replace("\r\n", "\n").replace('\r', '\n').split("\n")
        val out = SpannableStringBuilder()
        var i = 0

        fun block(content: CharSequence, vararg spans: Any) {
            if (out.isNotEmpty()) out.append("\n\n")
            val start = out.length
            out.append(content)
            spans.forEach { out.setSpan(it, start, out.length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE) }
        }

        while (i < lines.size) {
            val line = lines[i]

            // 代码块
            if (FENCE.containsMatchIn(line)) {
                val buffer = mutableListOf<String>()
                i++
                while (i < lines.size && !FENCE.containsMatchIn(lines[i])) buffer.add(lines[i++])
                i++ // 吃掉收尾的 ```
                block(
                    buffer.joinToString("\n"),
                    TypefaceSpan("monospace"),
                    BackgroundColorSpan(CODE_BACKGROUND),
                    LeadingMarginSpan.Standard(LIST_INDENT_PX / 2),
                )
                continue
            }

            if (line.isBlank()) { i++; continue }

            // 分隔线
            if (HR.containsMatchIn(line)) {
                block("──────────", RelativeSizeSpan(0.9f))
                i++
                continue
            }

            // 标题
            val heading = HEADING.find(line)
            if (heading != null) {
                val level = minOf(heading.groupValues[1].length + 2, 6)
                block(
                    inline(heading.groupValues[2].trim()),
                    StyleSpan(Typeface.BOLD),
                    RelativeSizeSpan(HEADING_SCALE[level] ?: 1.0f),
                )
                i++
                continue
            }

            // 引用
            if (QUOTE.containsMatchIn(line)) {
                val buffer = mutableListOf<String>()
                while (i < lines.size && QUOTE.containsMatchIn(lines[i])) {
                    buffer.add(lines[i++].replaceFirst(QUOTE, ""))
                }
                block(render(buffer.joinToString("\n")), QuoteSpan(QUOTE_STRIPE))
                continue
            }

            // 列表
            if (UL.containsMatchIn(line) || OL.containsMatchIn(line)) {
                val ordered = !UL.containsMatchIn(line)
                val marker = if (ordered) OL else UL
                val items = mutableListOf<CharSequence>()
                while (i < lines.size && marker.containsMatchIn(lines[i])) {
                    val buffer = mutableListOf(lines[i++].replaceFirst(marker, ""))
                    // 缩进的续行并进同一条
                    while (i < lines.size && CONTINUATION.containsMatchIn(lines[i]) &&
                        !UL.containsMatchIn(lines[i]) && !OL.containsMatchIn(lines[i])
                    ) {
                        buffer.add(lines[i++].trim())
                    }
                    items.add(inline(buffer.joinToString(" ")))
                }
                val body = SpannableStringBuilder()
                items.forEachIndexed { index, item ->
                    if (index > 0) body.append("\n")
                    body.append(if (ordered) "${index + 1}. " else "• ").append(item)
                }
                block(body, LeadingMarginSpan.Standard(0, LIST_INDENT_PX))
                continue
            }

            // 段落：连续非空行合成一段，行内换行保留
            val paragraph = mutableListOf<String>()
            while (i < lines.size && lines[i].isNotBlank() && !BLOCK_START.containsMatchIn(lines[i]) &&
                !UL.containsMatchIn(lines[i]) && !OL.containsMatchIn(lines[i])
            ) {
                paragraph.add(lines[i++])
            }
            val body = SpannableStringBuilder()
            paragraph.forEachIndexed { index, text ->
                if (index > 0) body.append("\n")
                body.append(inline(text.trim()))
            }
            block(body)
        }
        return out
    }

    /**
     * 行内规则。逐字符扫，不像网页端那样靠占位符轮流替换：那套是为了让后面的正则
     * 不再碰已经生成的 <a>，这里正着扫一遍就没这个问题，顺带把 `**连着 *斜体* 的粗体**`
     * 这类嵌套递归处理了。
     */
    private fun inline(text: String): CharSequence {
        val out = SpannableStringBuilder()
        var i = 0

        fun styled(content: String, vararg spans: Any) {
            val start = out.length
            out.append(inline(content))
            spans.forEach { out.setSpan(it, start, out.length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE) }
        }

        fun link(label: String, href: String) {
            val start = out.length
            out.append(label)
            out.setSpan(URLSpan(href), start, out.length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
        }

        while (i < text.length) {
            val rest = text.substring(i)
            val char = text[i]

            // 行内代码最先认，里面的星号不算格式
            if (char == '`') {
                val end = text.indexOf('`', i + 1)
                if (end > i + 1) {
                    val start = out.length
                    out.append(text, i + 1, end)
                    out.setSpan(TypefaceSpan("monospace"), start, out.length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
                    out.setSpan(BackgroundColorSpan(CODE_BACKGROUND), start, out.length, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
                    i = end + 1
                    continue
                }
            }

            // [文字](链接)。带 ! 的图片写法也认——手机卡片里内联不了图，退成链接
            if (char == '[' || (char == '!' && rest.startsWith("!["))) {
                val match = LINK.find(rest)
                if (match != null) {
                    val href = safeUrl(match.groupValues[2])
                    val label = match.groupValues[1].ifBlank { match.groupValues[2] }
                    if (href != null) link(label, href) else out.append(inline(label))
                    i += match.value.length
                    continue
                }
            }

            // 裸链接
            if ((char == 'h' || char == 'H') && BARE_URL.containsMatchIn(rest)) {
                var url = BARE_URL.find(rest)!!.value.replace(URL_TAIL, "")
                // 「（见 https://a.cn/x)」这种，多出来的右括号还给正文
                while (url.endsWith(")") && url.count { it == ')' } > url.count { it == '(' }) {
                    url = url.dropLast(1)
                }
                val href = safeUrl(url)
                if (href != null) {
                    link(url, href)
                    i += url.length
                    continue
                }
            }

            val strong = rest.startsWith("**") || rest.startsWith("__")
            if (strong || rest.startsWith("~~")) {
                val fence = rest.substring(0, 2)
                val end = rest.indexOf(fence, 2)
                if (end > 2 && !rest.substring(2, end).contains('\n')) {
                    styled(
                        rest.substring(2, end),
                        if (strong) StyleSpan(Typeface.BOLD) else StrikethroughSpan(),
                    )
                    i += end + 2
                    continue
                }
            }

            // 单个 * 或 _ 是斜体。`_` 额外要求前面不是字母数字，免得动了 snake_case
            if (char == '*' || (char == '_' && (i == 0 || !text[i - 1].isLetterOrDigit()))) {
                val end = rest.indexOf(char, 1)
                if (end > 1 && !rest.substring(1, end).contains('\n')) {
                    styled(rest.substring(1, end), StyleSpan(Typeface.ITALIC))
                    i += end + 1
                    continue
                }
            }

            out.append(char)
            i++
        }
        return out
    }
}
