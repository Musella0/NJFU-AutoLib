# AutoLib Docker 部署说明

本家： [lnk137/NJFU-AutomaticLibrary](https://github.com/lnk137/NJFU-AutomaticLibrary) 通过肘击Claude二创出来的预约小服务器

- 用户端 Web 页面：配置多个学号、设置预约规则、立即抢座、查看结果
- 管理后台：TOTP 二步验证登录、公告管理、用户开关与保护策略管理
- 定时任务：按设定时间自动预约，并持续执行迟到保护与自动午休检查
- MongoDB：持久化存储账号配置、座位信息、公告和预约结果

## 架构

```text
┌──────────────────────────────────────────────────────────────┐
│ docker compose                                               │
│                                                              │
│  ┌────────────────┐        ┌──────────────────────────────┐  │
│  │ flask-api      │───────▶│ 用户端 / 管理后台 / REST API │  │
│  │ :5004          │        └──────────────────────────────┘  │
│  └──────┬─────────┘                                           │
│         │                                                     │
│         ▼                                                     │
│  ┌──────────────┐        ┌─────────────────────────────────┐  │
│  │ mongo        │◀──────▶│ 用户配置 / 座位数据 / 公告 / 结果 │  │
│  │ :27017(内网) │        └─────────────────────────────────┘  │
│  └──────────────┘                                           │
│         ▲                                                     │
│         │                                                     │
│  ┌──────────────┐        ┌─────────────────────────────────┐  │
│  │ scheduler    │───────▶│ 每日预约 / 迟到保护 / 自动午休检查 │  │
│  └──────────────┘        └─────────────────────────────────┘  │
│                                                              │
│  ┌──────────────┐                                             │
│  │ seed         │  一次性导入座位信息到 MongoDB               │
│  └──────────────┘                                             │
└──────────────────────────────────────────────────────────────┘
```

## 服务说明

| 服务 | 说明 |
|------|------|
| `mongo` | MongoDB 7，持久化存储用户配置、座位信息、公告和结果 |
| `flask-api` | Flask Web/API 服务，默认监听 `5004` |
| `scheduler` | APScheduler 定时任务进程，负责每日预约与保护逻辑 |
| `seed` | 一次性初始化服务，将 `backend/座位信息/` 导入数据库 |

## 当前功能

### 用户端

- 首页 `/`：查看今日/明日预约状态、公告、结果摘要
- 多账号管理：一个 Web 用户下可绑定多个学号
- 支持游客会话：未注册时也会分配临时 `guest_uid`
- 预约配置：
  - 按星期配置时间段
  - 统一时段模式
  - 一天多段预约
  - 座位优先级列表
- 凭据验证：分别校验 VPN 密码和图书馆密码
- 手动操作：
  - 立即执行一次预约
  - 查询当前预约
  - 取消预约
  - 自定义今日时段预约
  - 午休重约
- 结果聚合：查看当前用户下全部学号的最新预约结果

### 管理端

- 管理后台 `/admin`
- 基于 TOTP 的二步验证登录
- 公告管理：创建、编辑、删除、置顶、启停
- 用户管理：
  - 查看所有用户配置
  - 开关自动预约 / 迟到保护
  - 调整保护策略
  - 删除用户配置

### 后台任务

- 按 `SCHEDULE_HOUR:SCHEDULE_MINUTE` 每日执行自动预约
- 预约后调度迟到保护
- 每分钟扫描自动午休任务
- 结果写回 MongoDB，供前台与后台读取

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

至少修改下面几项：

- `MONGO_PASS`：MongoDB 密码
- `SECRET_KEY`：Flask Session 密钥，建议 `openssl rand -hex 32`
- `ENCRYPTION_KEY`：凭据加密密钥，建议 `openssl rand -hex 32`
- `ADMIN_TOTP_SECRET`：管理员二步验证密钥，不配置则无法登录 `/admin`

`.env.example` 中的主要变量如下：

| 变量 | 说明 |
|------|------|
| `MONGO_USER` | MongoDB 用户名 |
| `MONGO_PASS` | MongoDB 密码 |
| `API_PORT` | Web/API 暴露端口，默认 `5004` |
| `SECRET_KEY` | Flask Session 签名密钥 |
| `SESSION_COOKIE_SECURE` | HTTPS 反代后建议设为 `true` |
| `ADMIN_TOTP_SECRET` | 管理后台 TOTP 密钥 |
| `ENCRYPTION_KEY` | 加密保存 VPN / 图书馆密码 |
| `SCHEDULE_HOUR` | 每日预约小时 |
| `SCHEDULE_MINUTE` | 每日预约分钟 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` | 邮件通知配置，可选 |

### 2. 启动服务

```bash
docker compose up -d --build
```

首次启动会发生这些事：

1. 构建 `backend` 镜像
2. 启动 MongoDB
3. 启动 `flask-api`
4. 启动 `scheduler`
5. 运行一次 `seed` 导入座位信息

### 3. 检查服务状态

```bash
docker compose ps
docker compose logs -f flask-api
docker compose logs -f scheduler
```

### 4. 打开页面

- 用户端：`http://localhost:5004/`
- 管理后台：`http://localhost:5004/admin`

## 初始化与验证

### 验证 API 是否正常

```bash
curl http://localhost:5004/
curl http://localhost:5004/api/announcements
curl http://localhost:5004/api/public/seats
```

### 检查座位数据是否导入

```bash
docker compose exec mongo mongosh -u autolib -p '你的Mongo密码' --authenticationDatabase admin --eval "db.getSiblingDB('AutoLib').devices.countDocuments()"
```

### 管理后台 TOTP 配置

如果已经在 `.env` 中设置好 `ADMIN_TOTP_SECRET`，启动后可直接访问 `/admin` 并用验证器 App 生成 6 位动态码登录。

常见做法：

1. 先生成一个 TOTP 密钥并写入 `.env`
2. `docker compose up -d --build`
3. 访问 `/admin`
4. 登录后点击 “TOTP 设置” 获取二维码并绑定到验证器

## 用户侧 API 概览

当前项目的主要接口已经迁移到 `/api/*`，而不是旧版的 `/db/*`。

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/auth/register` | 注册 Web 用户 |
| `POST` | `/api/auth/login` | 登录 Web 用户 |
| `POST` | `/api/auth/logout` | 退出登录 |
| `GET` | `/api/auth/me` | 查看当前登录/游客状态 |

### 我的账号

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/my/accounts` | 获取当前用户下全部学号配置 |
| `GET` | `/api/my/accounts/<pid>` | 获取指定学号配置 |
| `POST` | `/api/my/accounts/<pid>` | 新增或更新学号配置 |
| `DELETE` | `/api/my/accounts/<pid>` | 删除学号配置 |
| `POST` | `/api/my/accounts/<pid>/verify` | 验证 VPN / 图书馆密码 |
| `POST` | `/api/my/accounts/<pid>/reserve_now` | 立即抢一次 |
| `POST` | `/api/my/accounts/<pid>/reserve_custom` | 按指定时段预约今天 |
| `GET` | `/api/my/accounts/<pid>/reservations` | 查询当前预约 |
| `POST` | `/api/my/accounts/<pid>/cancel` | 取消预约 |
| `GET` | `/api/my/accounts/<pid>/result` | 获取最新结果 |
| `GET,POST` | `/api/my/accounts/<pid>/nap_config` | 获取/保存午休配置 |
| `POST` | `/api/my/accounts/<pid>/nap` | 执行午休重约 |
| `POST` | `/api/my/accounts/<pid>/arrived` | 切换“今日已到馆”标记 |
| `GET` | `/api/my/reservation_results` | 聚合当前用户的全部预约结果 |

### 公共与管理员接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/public/seats` | 获取公开座位列表 |
| `GET` | `/api/announcements` | 获取启用中的公告 |
| `POST` | `/api/admin/login` | 管理员 TOTP 登录 |
| `GET` | `/api/admin/me` | 查看管理员登录状态 |
| `POST` | `/api/admin/logout` | 管理员退出 |
| `GET` | `/api/admin/announcements` | 获取全部公告 |
| `POST` | `/api/admin/announcements` | 新建公告 |
| `PUT` | `/api/admin/announcements/<id>` | 更新公告 |
| `DELETE` | `/api/admin/announcements/<id>` | 删除公告 |
| `GET` | `/api/users` | 获取全部用户配置 |
| `POST` | `/api/users/<pid>/toggle` | 切换自动预约或迟到保护 |
| `POST` | `/api/users/<pid>/update` | 更新用户配置字段 |
| `POST` | `/api/users/<pid>/protection_settings` | 更新保护策略 |
| `DELETE` | `/api/users/<pid>` | 删除用户配置 |

## 配置示例

### 保存一个学号配置

```bash
curl -X POST http://localhost:5004/api/my/accounts/2023000001 \
  -H "Content-Type: application/json" \
  -d '{
    "vpn_password": "你的VPN密码",
    "lib_password": "你的图书馆密码",
    "seat_list": ["3F-A001", "3F-A002", "3F-A003"],
    "mode": "week_time",
    "time": {
      "week_time": {
        "1": ["08:00-12:00", "14:00-22:00"],
        "2": ["08:00-22:00"],
        "3": ["08:00-22:00"],
        "4": ["08:00-22:00"],
        "5": ["08:00-20:00"],
        "6": ["08:00-22:00"],
        "7": []
      }
    },
    "is_reserved": "True",
    "late_protection": "True"
  }'
```

说明：

- `seat_list` 使用座位名称，不是内部 `devId`
- `mode` 目前支持 `week_time`、`tomorrow`、`after_tomorrow`
- `time` 支持字符串单段，也支持数组多段
- 周五若结束时间晚于 `20:00`，调度逻辑会自动截断到 `20:00`

### 立即执行一次预约

```bash
curl -X POST http://localhost:5004/api/my/accounts/2023000001/reserve_now
```

### 查询最新预约结果

```bash
curl http://localhost:5004/api/my/accounts/2023000001/result
```

## 运维命令

```bash
docker compose ps
docker compose logs -f
docker compose logs -f flask-api
docker compose logs -f scheduler
docker compose restart
docker compose restart flask-api
docker compose restart scheduler
docker compose build --no-cache
docker compose up -d
docker compose run --rm seed
docker compose down
docker compose down -v
```

## 目录结构

```text
autolib/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
├── README.md
└── backend/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py
    ├── scheduled_task.py
    ├── scheduler_runner.py
    ├── seed_devices.py
    ├── blueprints/
    │   └── database_bp.py
    ├── templates/
    │   ├── index.html
    │   └── admin.html
    ├── static/
    │   ├── app.js
    │   └── styles.css
    ├── utils/
    │   ├── config.py
    │   ├── crypto.py
    │   ├── library_system.py
    │   ├── notify.py
    │   ├── vpn_system.py
    │   └── ...
    └── 座位信息/
        ├── 二楼A区座位.txt
        ├── 三楼A区座位.txt
        └── ...
```

## 注意事项

### 1. `/db/*` 是旧接口

仓库里仍然保留了 `backend/blueprints/database_bp.py` 这套旧接口，但当前实现已经把主要用户流程迁移到了 `/api/*`。

另外，`/db/*` 现在默认要求管理员会话，已经不适合作为用户侧公开接入方式。新接入请优先使用 `/api/*`。

### 2. 密码会加密存储，但不是哈希

VPN 密码和图书馆密码会通过 `ENCRYPTION_KEY` 做可逆加密，以便后台任务执行自动登录。请务必：

- 使用足够强的 `ENCRYPTION_KEY`
- 保护好 `.env`
- 不要把 `.env` 提交到仓库

### 3. 生产部署建议放在反向代理后

如果对外提供服务，建议配合 Nginx / Caddy 使用 HTTPS，并设置：

```env
SESSION_COOKIE_SECURE=true
```

## 后续可做

- 补一份面向开发者的本地调试说明
- 为主要 API 增加自动化测试
- 继续清理旧版 `/db/*` 接口，避免文档和实现再次分叉
