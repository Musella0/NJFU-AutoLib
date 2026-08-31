package com.autolib.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors

data class ApiResponse(
    val code: Int,
    val body: String,
    val jsonObject: JSONObject? = null,
    val jsonArray: JSONArray? = null,
) {
    val ok: Boolean get() = code in 200..299
    fun message(fallback: String): String =
        jsonObject?.optString("error")?.takeIf { it.isNotBlank() }
            ?: jsonObject?.optString("message")?.takeIf { it.isNotBlank() }
            ?: fallback
}

/** Small JSON client used directly by the native Android screens. */
class NativeApi(context: Context) {
    private val prefs = context.getSharedPreferences("native_api", Context.MODE_PRIVATE)
    private val executor = Executors.newFixedThreadPool(3)
    /** 顺序敏感的写入（座位优先级等）走单线程，避免快速连点后请求乱序落库。 */
    private val serialExecutor = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val baseUrl = BuildConfig.SERVER_URL.trimEnd('/')

    fun get(path: String, callback: (ApiResponse) -> Unit) = request("GET", path, null, false, callback)
    fun post(path: String, body: JSONObject = JSONObject(), callback: (ApiResponse) -> Unit) =
        request("POST", path, body, false, callback)
    fun postSerial(path: String, body: JSONObject, callback: (ApiResponse) -> Unit) =
        request("POST", path, body, true, callback)
    fun delete(path: String, callback: (ApiResponse) -> Unit) = request("DELETE", path, null, false, callback)

    fun encoded(value: String): String = URLEncoder.encode(value, StandardCharsets.UTF_8.name())

    /**
     * 同步发起请求，调用方必须已经在后台线程。
     * 供闹钟触发的后台同步和小组件刷新使用——那些场景没有 Activity 可以回调。
     */
    fun getBlocking(path: String, readTimeoutMs: Int = 20_000): ApiResponse =
        execute("GET", path, null, readTimeoutMs)

    /** 同上，小组件按钮（如「我已到馆」）在后台线程直接调用。 */
    fun postBlocking(path: String, body: JSONObject = JSONObject(), readTimeoutMs: Int = 20_000): ApiResponse =
        execute("POST", path, body, readTimeoutMs)

    /**
     * 取二进制资源（平面图底图）。失败一律回 null——底图缺失只是让选座图不可用，
     * 不该像接口错误那样弹提示，所以这里不复用 ApiResponse 的错误通道。
     * 调用方必须已经在后台线程。
     */
    fun getBytesBlocking(path: String, readTimeoutMs: Int = 30_000): ByteArray? {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(baseUrl + path).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                connectTimeout = 15_000
                readTimeout = readTimeoutMs
                setRequestProperty("User-Agent", "AutoLib-Android/${BuildConfig.VERSION_NAME}")
            }
            if (connection.responseCode !in 200..299) return null
            connection.inputStream.use { it.readBytes() }
        } catch (error: Exception) {
            null
        } finally {
            connection?.disconnect()
        }
    }

    private fun request(
        method: String,
        path: String,
        body: JSONObject?,
        serial: Boolean,
        callback: (ApiResponse) -> Unit,
    ) {
        (if (serial) serialExecutor else executor).execute {
            val response = execute(method, path, body, 90_000)
            main.post { callback(response) }
        }
    }

    private fun execute(
        method: String,
        path: String,
        body: JSONObject?,
        readTimeoutMs: Int,
    ): ApiResponse {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(baseUrl + path).openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 15_000
                readTimeout = readTimeoutMs
                setRequestProperty("Accept", "application/json")
                setRequestProperty("User-Agent", "AutoLib-Android/${BuildConfig.VERSION_NAME}")
                prefs.getString("cookie", null)?.takeIf { it.isNotBlank() }?.let {
                    setRequestProperty("Cookie", it)
                }
                if (body != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                }
            }
            body?.let {
                connection.outputStream.use { stream ->
                    stream.write(it.toString().toByteArray(StandardCharsets.UTF_8))
                }
            }
            val code = connection.responseCode
            persistCookie(connection.headerFields["Set-Cookie"])
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.use { input ->
                BufferedReader(InputStreamReader(input, StandardCharsets.UTF_8)).readText()
            }.orEmpty()
            parse(code, text)
        } catch (error: Exception) {
            ApiResponse(-1, error.message.orEmpty(), JSONObject().put("error", error.message ?: "网络连接失败"))
        } finally {
            connection?.disconnect()
        }
    }

    /**
     * 按 name 合并 Set-Cookie，而不是整体覆盖：退出登录时服务器会下发空值的
     * session cookie，直接覆盖会把 `session=` 这样的空壳保存下来一直发送。
     */
    private fun persistCookie(headers: List<String>?) {
        val incoming = headers.orEmpty()
            .map { it.substringBefore(';').trim() }
            .filter { it.contains('=') }
        if (incoming.isEmpty()) return
        val jar = LinkedHashMap<String, String>()
        prefs.getString("cookie", null).orEmpty()
            .split(';')
            .map { it.trim() }
            .filter { it.contains('=') }
            .forEach { jar[it.substringBefore('=')] = it.substringAfter('=') }
        incoming.forEach {
            val name = it.substringBefore('=')
            val value = it.substringAfter('=')
            if (value.isBlank()) jar.remove(name) else jar[name] = value
        }
        prefs.edit()
            .putString("cookie", jar.entries.joinToString("; ") { "${it.key}=${it.value}" })
            .apply()
    }

    private fun parse(code: Int, text: String): ApiResponse {
        val trimmed = text.trim()
        return when {
            trimmed.startsWith("{") -> ApiResponse(code, text, jsonObject = runCatching { JSONObject(trimmed) }.getOrNull())
            trimmed.startsWith("[") -> ApiResponse(code, text, jsonArray = runCatching { JSONArray(trimmed) }.getOrNull())
            else -> ApiResponse(code, text)
        }
    }
}
