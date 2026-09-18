# 手机中继：让每个用户的图书馆请求从自己的手机出去

## 为什么

2026-09-17 下午起，统一身份认证（CAS）对 webvpn 这个系统要求短信动态码二次认证，
服务器上的自动登录全部失效。同一天实测：**校园网里直接打开 `libseat.njfu.edu.cn`，
走 CAS 登录不要短信码**——二次认证只挂在 webvpn 上。

所以出路是「不经 webvpn，从校园网里出去」。用一台校内机器当统一出口不行：校园网实名，
十几个账号每天 7:00:00 从同一个宿舍 IP 密集登录 CAS，查到就是一个具体的人。
于是改成**每个人的流量从自己的手机出去**：

```
境内 scheduler / api ──内部 HTTP──▶ relay ──WebSocket──▶ 手机 App ──校园 WiFi──▶ libseat / authserver
     脑子不动                       连接池           只转发 HTTP 请求/响应
```

- 服务器把每个 HTTP 请求（方法、URL、头、体）推给该学号的手机，手机在校园 WiFi 上原样发出去，
  把状态码、响应头、响应体原样送回。手机**不跟随跳转、不存 cookie**，会话仍由服务端 `requests` 管。
- 业务逻辑一行不动：CAS 登录、ic-web 预约、13:58 占位、迟到保护、学习记录、多人协调全部原样跑。
- 每个人的请求从自己的手机、自己的学号出去，和他本人用图书馆系统没有区别，互相不牵连。

## 服务端组成（本次已完成）

| 文件 | 作用 |
|---|---|
| `backend/utils/phone_relay.py` | `LIBRARY_ACCESS` 三种模式；`PhoneRelayAdapter`（requests 适配器，把请求交给 relay）；`PhoneOfflineError` |
| `backend/relay_server.py` | 新容器 `relay`（aiohttp）：收手机的 WebSocket，给 api/scheduler 提供 `/internal/request` |
| `backend/relay_client.py` | 协议的 Python 参考客户端，Termux/电脑上就能当某个学号的出口；Android 端照着它写 |
| `backend/utils/cas_login.py` | CAS 登录页解析/填表，webvpn 与直连共用（从 `vpn_system.py` 拆出） |
| `backend/utils/vpn_system.py` | 直连模式下 `vpn_login()` 改为：问 libseat 要 SSO 入口 → CAS 填表 → 拿 ticket |
| `backend/utils/library_system.py` | 直连模式的 URL（无 webvpn 包装）；SSO 链中途撞上 CAS 登录页会自己填表；密码被拒抛 `LibraryCredentialsError` |
| `backend/utils/base_system.py` | 建会话时按学号挂上手机出口 |
| `backend/main.py` | `verify` 支持 `X-Relay-Session`（首次验证时手机还没令牌，先匿名连）；新增 `GET /api/my/accounts/<pid>/phone` |
| `backend/scheduled_task.py` | 6:50 预登录发现手机不在线，提醒用户一次 |
| `docker-compose*.yml` / `Caddyfile` | `relay` 服务、`RELAY_*` 变量、`/relay/*` 路由 |

### 模式开关

```
LIBRARY_ACCESS=webvpn   默认。2026-09-17 之前的行为，自己部署的人什么都不用改
LIBRARY_ACCESS=direct   服务器直连 libseat / authserver（只有校园网里的机器能用；调试直连 CAS 逻辑用）
LIBRARY_ACCESS=phone    经用户手机转发
```

phone 模式下只有 `libseat.njfu.edu.cn` 和 `authserver.njfu.edu.cn` 两个域名的请求经手机；
邮件、LLM 等照常直连。手机不在线时请求立刻失败（`PhoneOfflineError`，文案「手机不在线：AutoLib App
没有连上服务器…」），不重试、不回退——没有任何回退能绕过去。

### WebSocket 协议

```
GET /relay/ws            Authorization: Bearer <API 令牌>   → 绑定学号
                         不带令牌                            → 匿名连接，只能配合 verify 用
服务器 → 手机  {"type":"welcome","pid":..,"relay_session":..,"heartbeat_seconds":30}
               {"type":"request","id":..,"method":..,"url":..,"headers":[[k,v]..],"body":b64|null,"timeout_ms":..}
手机 → 服务器  {"type":"hello","app_version":..,"network":..}
               {"type":"response","id":..,"status":..,"reason":..,"headers":[[k,v]..],"body":b64}
               {"type":"error","id":..,"message":..}
```

手机端四条规矩：不跟随跳转；不存 cookie；响应体解压后再回；去掉 `Content-Encoding` /
`Content-Length` / `Transfer-Encoding`。同名响应头（多条 `Set-Cookie`）逐条回，别合并。
同一学号新连接会顶掉旧连接。心跳由服务器发 ping（Cloudflare 约 100 秒空闲就掐，30 秒一次）。

### 首次验证（手机还没有令牌）

1. App 不带令牌连 `/relay/ws`，welcome 里拿到 `relay_session`
2. `POST /api/my/accounts/<pid>/verify` 时带头 `X-Relay-Session: <relay_session>`，
   本次验证的 CAS + 图书馆请求就经这条匿名连接转发
3. 验证通过后服务器把这条连接认领给该学号，同时回令牌；App 之后用令牌重连即可

已经验证过的老用户手机不在线时，网站登录仍走本地缓存放行（`offline: true`），
响应多一个 `phone_offline: true`。

## 部署（backend + edge）

```bash
# backend
cd autolib && git pull
echo 'LIBRARY_ACCESS=phone' >> .env
echo 'RELAY_BIND=10.8.0.2'  >> .env
docker compose build relay flask-api scheduler
docker compose up -d relay flask-api        # scheduler 等确认链路通了再 start
curl -s http://127.0.0.1:5005/relay/healthz  # 本机不通是正常的：只绑了隧道地址
curl -s http://10.8.0.2:5005/relay/healthz   # {"ok":true,"phones":0}

# edge
cd autolib && git pull
echo 'RELAY_UPSTREAM=10.8.0.2:5005' >> .env
echo 'LIBRARY_ACCESS=phone' >> .env          # 探针也经观测账号的手机
docker compose -p autolib -f docker-compose.edge.yml up -d caddy
curl -s https://你的域名/relay/healthz
```

## 联调（Android 端写好之前）

在校园网里任何一台机器（Termux 也行）：

```bash
pip install requests websockets
python relay_client.py --server wss://你的域名/relay/ws --token <你的 API 令牌>
```

令牌是 `verify` 接口回的 `token`。连上后 backend 上 `docker logs autolib-relay` 能看到「手机上线」，
然后：

```bash
docker exec autolib-scheduler python -c "
from scheduled_task import login_library, get_all_active_reservations
cfg = next(c for c in get_all_active_reservations() if c['pid'] == '<学号>')
lib = login_library(cfg); print('OK', lib.user_info and lib.user_info.get('name'))"
```

看 relay_client 那边打出的请求序列：`auth/address` → `authserver/login`（GET 200）→ `authserver/login`（POST 302）
→ `ic-web/auth/token` → … → `auth/userInfo` 200。整条通了再 `docker compose start scheduler`。

## 接受的代价

- 只有装了 Android App 的人能用；只用网页和 iOS 用户救不回来（浏览器有 CORS，做不了转发）。
- 手机 7:00 得在校园 WiFi 上、App 没被 ROM 杀掉。6:50 预登录发现手机不在线会提醒一次，
  之后就只能靠 App 自己的前台服务 + 精确闹钟。
- 每个请求多一跳（服务器 → 手机 → 图书馆），几十毫秒。
