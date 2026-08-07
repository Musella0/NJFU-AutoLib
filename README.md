# AutoLib — 南京林业大学图书馆自动抢座系统

> 原作：[lnk137/NJFU-AutomaticLibrary](https://github.com/lnk137/NJFU-AutomaticLibrary)，基于肘击Claude做出的二创版本。

---

## 这个项目是干什么的？

每天抢图书馆座位太麻烦？AutoLib 帮你**全自动搞定**。

你只需要在网页上配置一次：学号、密码、想坐哪、几点到几点，之后每天到了预约时间，系统会自动帮你抢好座位，抢完还能发通知告诉你结果。

---

## 快速开始
### 方法1 直接使用
直接登陆网址：[南林图书馆.中国](https://南林图书馆.中国)

安卓端也可以选择下载[船新版本](../../releases/latest)；也可以选择[蓝奏云](https://wwbqs.lanzouq.com/ioEtx4126v1g) (注意蓝奏云之后只会停留在0.2.1版本，最新请在release中下载或下载后点击检查更新)

### 方法2 自己部署
可以将以下提示词复制喂给AI，让AI帮你部署：

```
请帮我把 AutoLib 部署到我自己的服务器上。仓库：https://github.com/Musella0/NJFU-AutoLib这是一个图书馆自动抢座系统，Flask + MongoDB + Caddy，用 Docker Compose 编排。
先克隆下来读 `quickstart.md`、`docker-compose.yml`、`.env.example` 和 `Caddyfile`，按你理解的正确顺序执行。

动手前先问我四件事：**域名**（要提前解析到这台服务器，Caddy 靠它自动签 HTTPS 证书）、**服务器前面是否还有 CDN 或另一层反向代理**、**是否需要邮件通知**（需要的话我给你 Resend 或 SMTP 配置）、**是否需要配置 Android 客户端**（需要的话我只提供自己的服务器地址，改哪里、怎么编译你自己判断）。

第四问我若回答「需要」，就用我给的地址替换掉客户端里写死的服务端常量并编译出可安装的包，中间不用再问我细节。只提示两点：客户端强制 HTTPS，自签证书要额外配置信任；release 签名口令来自不在版本库里的 `android/local.properties`，缺失时产出的是无法直接安装的未签名包。

过程中不要把密钥、密码打印到终端输出里，写进文件即可；用中文跟我交流。

部署完提醒我：这个项目会代替用户登录学校账号并自动抢座，需自行确认符合学校规定，并对存储他人凭据承担责任。
```

详细部署步骤见 [quickstart.md](quickstart.md)。

自己古典部署的简要流程：

```bash
# 1. 复制并填写环境变量
cp .env.example .env
# 编辑 .env，填写 MONGO_PASS、SECRET_KEY、ENCRYPTION_KEY、ADMIN_TOTP_SECRET

# 2. 启动所有服务
docker compose up -d --build

# 3. 打开浏览器
# 用户端：http://localhost:5004/
# 管理后台：http://localhost:5004/admin
```

### 密码怎么保存的？安全吗？

用户的 VPN 密码和图书馆密码需要存下来（因为自动登录时要用），所以用的是**可逆加密**（AES），而不是哈希。

- 加密密钥来自环境变量 `ENCRYPTION_KEY`，不放在代码里
- 所有密码字段在存入数据库时加密，读出来后才解密，管理员也看不到明文
- 无需注册网站账号：添加学号时验证统一身份认证密码，通过即登录，数据跟随学号保存；同时缓存一份不可逆的密码哈希，学校服务不可用时可凭它进入（此时状态保持「未验证」，学校侧恢复后需重新验证）
---



## 注意事项

1. **VPN 密码 = 统一身份认证密码**（登网上办事大厅用的那个）
2. 密码是可逆加密保存的，务必保管好 `.env` 文件，不要提交到公开仓库
3. 生产环境建议配合 Nginx/Caddy 加 HTTPS，并设置 `SESSION_COOKIE_SECURE=true`
4. 周五图书馆 20:00 关门，预约结束时间超过 20:00 的会自动截断

[![](https://s41.ax1x.com/2026/05/17/pevTaxe.webp)](https://imgchr.com/i/pevTaxe)
