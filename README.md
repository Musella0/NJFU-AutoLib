# AutoLib — 南京林业大学图书馆自动抢座系统

> 原作：[lnk137/NJFU-AutomaticLibrary](https://github.com/lnk137/NJFU-AutomaticLibrary)，基于肘击Claude做出的二创的 Web 版本。

---

## 这个项目是干什么的？

每天抢图书馆座位太麻烦？AutoLib 帮你**全自动搞定**。

你只需要在网页上配置一次：学号、密码、想坐哪、几点到几点。之后每天到了预约时间，系统会自动登录你的账号，帮你抢好座位，抢完还能发通知告诉你结果。

---

## 快速开始
### 方法1 直接使用
直接登陆网址：[南林图书馆.中国](https://南林图书馆.中国)

安卓端也可以选择下载[船新版本](../../releases/latest)；也可以选择[蓝奏云](https://wwbqs.lanzouq.com/iWI1y3q8st5a)

~~（其实就是个套壳app，和网页访问没区别）~~

### 方法2 自己部署
详细部署步骤见 [quickstart.md](quickstart.md)。

简要流程：

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
- 网站登录密码（Web 账号）用的是不可逆的 bcrypt 哈希
- 管理后台使用 TOTP 二步验证
---


## 目录结构

```
autolib/
├── docker-compose.yml        # 生产环境编排文件
├── .env.example              # 环境变量模板
├── README.md
├── quickstart.md             # 详细部署教程
└── backend/
    ├── main.py               # Flask 应用入口，所有 API 路由
    ├── scheduled_task.py     # 预约、迟到保护、午休核心逻辑
    ├── scheduler_runner.py   # 定时任务调度入口（容器启动点）
    ├── seed_devices.py       # 座位数据初始化脚本
    ├── utils/
    │   ├── vpn_system.py     # VPN 登录模块
    │   ├── library_system.py # 图书馆系统交互模块
    │   ├── crypto.py         # 密码加解密
    │   ├── notify.py         # 邮件 + Server 酱通知
    │   └── config.py         # 配置读取
    ├── templates/            # 网页模板（index.html / admin.html）
    ├── static/               # 前端 JS / CSS
    └── 座位信息/             # 座位数据文件，启动时导入 MongoDB
```

---

## 注意事项

1. **VPN 密码 = 统一身份认证密码**（登 webVPN 用的那个），不是 Wi-Fi 密码
2. **图书馆密码 = IC 空间系统密码**，默认是 `njfu学号!`
3. 密码是可逆加密保存的，务必保管好 `.env` 文件，不要提交到公开仓库
4. 生产环境建议配合 Nginx/Caddy 加 HTTPS，并设置 `SESSION_COOKIE_SECURE=true`
5. 周五图书馆 20:00 关门，预约结束时间超过 20:00 的会自动截断

[![](https://s41.ax1x.com/2026/05/17/pevTaxe.webp)](https://imgchr.com/i/pevTaxe)
