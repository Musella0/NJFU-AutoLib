# 让 AI 帮你部署

把下面整段复制给有终端权限的 AI 助手（Claude Code / Cursor / Codex 等），
在服务器上运行，或让它能 SSH 到服务器。

---

请帮我把 AutoLib 部署到我自己的服务器上。仓库：https://github.com/Musella0/NJFU-AutoLib
这是一个图书馆自动抢座系统，Flask + MongoDB + Caddy，用 Docker Compose 编排。
先克隆下来读 `quickstart.md`、`docker-compose.yml`、`.env.example` 和 `Caddyfile`，
按你理解的正确顺序执行，每步验证结果再继续，失败就停下来告诉我原因。

动手前先问我三件事：**域名**（要提前解析到这台服务器，Caddy 靠它自动签 HTTPS 证书）、
**服务器前面是否还有 CDN 或另一层反向代理**、**是否需要邮件通知**（需要的话我给你 Resend 或 SMTP 配置）。

有几个坑文档里没写清楚，请注意：

1. `.env` 里的 `TRUSTED_PROXY_COUNT` 必须等于真实的反代层数——只有项目自带的 Caddy 就填 1，前面还有 Cloudflare 之类就填 2。填错会让接口限流按错误的 IP 计数，要么全站共用一个限流桶，要么可被伪造 `X-Forwarded-For` 绕过。
2. `ENCRYPTION_KEY` 用来加密用户的统一身份认证密码，**丢失或改动后数据库里已存的密码全部解不开**，所有用户都得重新添加学号。生成后提醒我单独备份，别和 `SECRET_KEY` 用同一个值。
3. 管理员账号不在 `.env` 里，要在服务起来后跑 `docker compose exec flask-api python init_admin.py` 交互式创建，会生成 TOTP 密钥且只显示一次。（`quickstart.md` 若提到 `ADMIN_TOTP_SECRET` 环境变量，那是过时说法，以脚本为准。）
4. `seed` 容器跑完就退出、状态是 `Exited (0)`，这是正常的一次性座位导入任务，不要当故障重启。
5. `SESSION_COOKIE_SECURE` 要和访问协议一致，HTTPS 填 `true`、纯 HTTP 填 `false`，否则表现为「登录后立刻掉线」。

如果我还要用 Android 客户端，注意服务端地址是编译期常量、默认指向原作者的服务器：改
`android/app/build.gradle.kts` 里的 `SERVER_URL` 为我的域名后重新编译。客户端只允许 HTTPS，
自签证书需要改 `network_security_config.xml`。

过程中不要把密钥、密码打印到终端输出里，写进文件即可；用中文跟我交流。
部署完提醒我：这个项目会代替用户登录学校账号并自动抢座，需自行确认符合学校规定，
并对存储他人凭据承担责任。
