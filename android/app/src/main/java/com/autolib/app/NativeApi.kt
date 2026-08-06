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
    private val main = Handler(Looper.getMainLooper())
    private val baseUrl = BuildConfig.SERVER_URL.trimEnd('/')

    fun get(path: String, callback: (ApiResponse) -> Unit) = request("GET", path, null, callback)
    fun post(path: String, body: JSONObject = JSONObject(), callback: (ApiResponse) -> Unit) =
        request("POST", path, body, callback)
    fun delete(path: String, callback: (ApiResponse) -> Unit) = request("DELETE", path, null, callback)

    fun encoded(value: String): String = URLEncoder.encode(value, StandardCharsets.UTF_8.name())

    private fun request(method: String, path: String, body: JSONObject?, callback: (ApiResponse) -> Unit) {
        executor.execute {
            val response = try {
                val connection = (URL(baseUrl + path).openConnection() as HttpURLConnection).apply {
                    requestMethod = method
                    connectTimeout = 15_000
                    readTimeout = 90_000
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
                connection.disconnect()
                parse(code, text)
            } catch (error: Exception) {
                ApiResponse(-1, error.message.orEmpty(), JSONObject().put("error", error.message ?: "网络连接失败"))
            }
            main.post { callback(response) }
        }
    }

    private fun persistCookie(headers: List<String>?) {
        val cookie = headers.orEmpty()
            .map { it.substringBefore(';') }
            .filter { it.contains('=') }
            .joinToString("; ")
        if (cookie.isNotBlank()) prefs.edit().putString("cookie", cookie).apply()
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
