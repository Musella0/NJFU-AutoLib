# NJFU-AutomaticLibrary Docker 部署

基于 [lnk137/NJFU-AutomaticLibrary](https://github.com/lnk137/NJFU-AutomaticLibrary) 改造的 Docker Compose 部署方案。

## 架构

```
┌─────────────────────────────────────────────┐
│  docker-compose                             │
│                                             │
│  ┌──────────┐  ┌───────────┐  ┌───────────┐ │
│  │ flask-api │  │ scheduler │  │  MongoDB  │ │
│  │  :5003    │──│  定时预约  │──│  :27017   │ │
│  └──────────┘  └───────────┘  └───────────┘ │
│                                    ▲        │
│  ┌──────────┐                      │        │
│  │   seed   │──── 初始化座位数据 ───┘        │
│  └──────────┘  (一次性运行)                  │
└─────────────────────────────────────────────┘
```

| 服务 | 说明 |
|------|------|
| `mongo` | MongoDB 7，持久化存储用户配置和座位数据 |
| `flask-api` | Flask 后端 API，供前端/手动调用 |
| `scheduler` | 每天定时执行预约 + 迟到保护 |
| `seed` | 一次性服务，导入座位信息到数据库 |

## 快速开始

### 1. 克隆并准备

```bash
# 把整个 autolib-docker 目录上传到服务器，然后：
cd autolib-docker

# 复制环境变量文件
cp .env.example .env

# 编辑配置（改密码、调整预约时间等）
nano .env
```

### 2. 启动

```bash
# 首次启动（会自动构建镜像、初始化座位数据）
docker compose up -d

# 查看日志
docker compose logs -f

# 单独看某个服务
docker compose logs -f scheduler
docker compose logs -f flask-api
```

### 3. 验证

```bash
# 检查服务状态
docker compose ps

# 测试 API
curl http://localhost:5003/

# 检查座位数据是否导入
docker compose exec mongo mongosh -u autolib -p autolib123 --eval "db.getSiblingDB('AutoLib').devices.countDocuments()"
```

## 配置说明

### .env 文件

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MONGO_USER` | autolib | MongoDB 用户名 |
| `MONGO_PASS` | autolib123 | MongoDB 密码（**请修改**） |
| `MONGO_PORT` | 27017 | MongoDB 外部端口 |
| `API_PORT` | 5003 | Flask API 外部端口 |
| `SCHEDULE_HOUR` | 19 | 每天几点执行预约 |
| `SCHEDULE_MINUTE` | 55 | 每天几分执行预约 |

### 添加用户预约

通过 API 添加预约配置：

```bash
curl -X POST http://localhost:5003/db/reservation/all \
  -H "Content-Type: application/json" \
  -d '{
    "pid": "你的学号",
    "vpn_password": "VPN密码",
    "lib_password": "图书馆密码",
    "seat_list": ["3F-A001", "3F-A002", "3F-A003"],
    "mode": "week_time",
    "time": {
      "week_time": {
        "1": "08:00-22:00",
        "2": "08:00-22:00",
        "3": "08:00-22:00",
        "4": "08:00-22:00",
        "5": "08:00-20:00",
        "6": "08:00-22:00",
        "7": "08:00-22:00"
      }
    },
    "priority": 1,
    "is_reserved": "True",
    "late_protection": "True"
  }'
```

> `seat_list` 填座位名称（如 `3F-A001`），系统会自动转为内部 ID。
> `mode` 可选 `week_time`（按星期分配时间）、`tomorrow`、`after_tomorrow`。

### 查询预约结果

```bash
curl -X POST http://localhost:5003/db/reservation/query \
  -H "Content-Type: application/json" \
  -d '{"pid": "你的学号"}'
```

## 运维

```bash
# 重启所有服务
docker compose restart

# 只重启调度器
docker compose restart scheduler

# 更新代码后重新构建
docker compose build --no-cache
docker compose up -d

# 重新导入座位数据
docker compose run --rm seed

# 停止所有服务
docker compose down

# 停止并删除数据（慎用）
docker compose down -v
```

## 目录结构

```
autolib-docker/
├── docker-compose.yml
├── .env.example
├── README.md
└── backend/
    ├── Dockerfile
    ├── .dockerignore
    ├── requirements.txt
    ├── main.py                  # Flask 入口
    ├── scheduled_task.py        # 预约核心逻辑
    ├── scheduler_runner.py      # 定时调度入口
    ├── seed_devices.py          # 座位数据初始化
    ├── blueprints/
    │   ├── database_bp.py       # 数据库 API
    │   └── app_bp.py            # 应用更新 API
    ├── utils/
    │   ├── config.py            # 配置（已改造支持 MongoDB 认证）
    │   ├── base_system.py
    │   ├── library_system.py
    │   ├── vpn_system.py
    │   ├── password_encryptor.py
    │   └── insert_seat_ifo.py
    └── 座位信息/
        ├── 二楼A区座位.txt
        ├── 三楼A区座位.txt
        └── ...
```
