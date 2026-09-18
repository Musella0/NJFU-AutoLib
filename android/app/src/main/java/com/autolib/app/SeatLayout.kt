package com.autolib.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * 图书馆平面图数据，对应网页端的 `loadSeatLayout()` / `/static/floorplans/`。
 *
 * 座位坐标是百分比（相对底图的宽和高），记的是圆点**左上角**——这是学校选座页
 * 自己的约定，画点和反查最近座位时都要补半个点才对得上。
 *
 * 一整套是 1 个 JSON + 12 张底图共 1.2MB，一学期也难得变一次，所以落盘缓存：
 * JSON 每 7 天回源一次，底图按文件名当不可变内容永久留着。
 */
object SeatLayout {
    private const val LAYOUT_PATH = "/static/floorplans/seat_layout.json"
    private const val IMAGE_PATH = "/static/floorplans/"
    private const val CACHE_DIR = "floorplans"
    private const val LAYOUT_FILE = "seat_layout.json"
    private const val LAYOUT_TTL_MS = 7L * 24 * 60 * 60 * 1000
    private const val BITMAP_CACHE_SIZE = 3

    data class Seat(val name: String, val x: Float, val y: Float)

    data class Area(
        val roomId: String,
        val location: String,
        val image: String,
        val seats: List<Seat>,
    ) {
        /** 用 location 而不是图纸上的 name：App 别处的区域下拉用的就是这套叫法。 */
        val label: String get() = "$location（${seats.size} 座）"
    }

    private val executor = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    @Volatile
    private var areas: List<Area> = emptyList()

    /** 底图解码后 1600×900 RGB_565 约 2.8MB，只留最近几张，够来回切换用。 */
    private val bitmaps = object : LinkedHashMap<String, Bitmap>(0, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Bitmap>) =
            size > BITMAP_CACHE_SIZE
    }

    /** 解析好的区域列表，失败或未下载到时是空表。回调总在主线程。 */
    fun areas(context: Context, api: NativeApi, callback: (List<Area>) -> Unit) {
        val loaded = areas
        if (loaded.isNotEmpty()) return callback(loaded)
        val appContext = context.applicationContext
        executor.execute {
            val parsed = parse(readLayout(appContext, api))
            if (parsed.isNotEmpty()) areas = parsed
            main.post { callback(parsed) }
        }
    }

    /** 座位号 → 它所在的区域，用来把已选的座位直接定位到图上。 */
    fun areaOfSeat(seatName: String): Area? =
        areas.firstOrNull { area -> area.seats.any { it.name == seatName } }

    /** 区域底图。下载或解码失败回 null，调用方自己提示。回调总在主线程。 */
    fun image(context: Context, api: NativeApi, area: Area, callback: (Bitmap?) -> Unit) {
        cachedBitmap(area.image)?.let { return callback(it) }
        val appContext = context.applicationContext
        executor.execute {
            val bitmap = decode(appContext, api, area.image)
            if (bitmap != null) synchronized(bitmaps) { bitmaps[area.image] = bitmap }
            main.post { callback(bitmap) }
        }
    }

    private fun cachedBitmap(image: String): Bitmap? = synchronized(bitmaps) { bitmaps[image] }

    private fun cacheDir(context: Context): File =
        File(context.cacheDir, CACHE_DIR).apply { mkdirs() }

    private fun readLayout(context: Context, api: NativeApi): String? {
        val file = File(cacheDir(context), LAYOUT_FILE)
        val fresh = file.isFile && System.currentTimeMillis() - file.lastModified() < LAYOUT_TTL_MS
        if (fresh) {
            val cached = runCatching { file.readText() }.getOrNull()
            if (cached != null) return cached
        }
        val bytes = api.getBytesBlocking(LAYOUT_PATH)
        if (bytes != null) {
            runCatching { file.writeBytes(bytes) }
            return String(bytes, Charsets.UTF_8)
        }
        // 回源失败就拿过期缓存顶上——离线时能看旧图，比打不开选座图强
        return runCatching { if (file.isFile) file.readText() else null }.getOrNull()
    }

    private fun parse(text: String?): List<Area> {
        if (text.isNullOrBlank()) return emptyList()
        val root = runCatching { JSONObject(text) }.getOrNull() ?: return emptyList()
        val list = root.optJSONArray("areas") ?: return emptyList()
        return (0 until list.length()).mapNotNull { index ->
            val item = list.optJSONObject(index) ?: return@mapNotNull null
            val image = item.optString("image")
            val raw = item.optJSONArray("seats")
            if (image.isBlank() || raw == null) return@mapNotNull null
            val seats = (0 until raw.length()).mapNotNull { i ->
                val entry = raw.optJSONArray(i) ?: return@mapNotNull null
                val name = entry.optString(0)
                if (name.isBlank() || entry.length() < 3) return@mapNotNull null
                Seat(name, entry.optDouble(1, 0.0).toFloat(), entry.optDouble(2, 0.0).toFloat())
            }
            if (seats.isEmpty()) return@mapNotNull null
            Area(
                roomId = item.opt("roomId")?.toString().orEmpty(),
                location = item.optString("location").ifBlank { item.optString("name") },
                image = image,
                seats = seats,
            )
        }
    }

    private fun decode(context: Context, api: NativeApi, image: String): Bitmap? {
        val file = File(cacheDir(context), image)
        // 线条施工图，RGB_565 看不出差别，内存却只要一半
        val options = BitmapFactory.Options().apply { inPreferredConfig = Bitmap.Config.RGB_565 }
        if (file.isFile) {
            val cached = runCatching { BitmapFactory.decodeFile(file.path, options) }.getOrNull()
            if (cached != null) return cached
            file.delete()   // 缓存坏了（下载中途断线），删掉重新拉
        }
        val bytes = api.getBytesBlocking(IMAGE_PATH + image) ?: return null
        val bitmap = runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) }
            .getOrNull() ?: return null
        runCatching { file.writeBytes(bytes) }
        return bitmap
    }
}
