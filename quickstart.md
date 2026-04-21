# AutoLib Docker 部署指南

> 本指南适用于 **Ubuntu / Debian** 系统。其他 Linux 发行版可参考本教程，自行肘击AI。

---

## 1. 安装 Docker

请按照 [Docker 官方安装指南](https://docs.docker.com/engine/install/ubuntu/) 安装 Docker。

> **注意**：不要安装 Docker Desktop，安装 Docker Engine 即可。

安装完成后，按照 [以非 root 用户身份管理 Docker](https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user) 完成安装后配置，避免后续命令需要频繁 `sudo`。

---

## 2. 安装 Git

```bash
sudo apt update && sudo apt install git -y
```

---

## 3. 克隆仓库

```bash
git clone https://github.com/Musella0/autolib.git && cd autolib
```

---

## 4. 配置环境变量

复制示例配置文件：

```bash
cp .env.example .env
```

用编辑器打开 `.env`，至少修改以下几项：

```bash
nano .env
```

| 变量 | 说明 |
|------|------|
| `MONGO_PASS` | MongoDB 密码，自定义一个强密码 |
| `SECRET_KEY` | Flask Session 密钥，运行下方命令生成 |
| `ENCRYPTION_KEY` | 凭据加密密钥，运行下方命令生成 |
| `ADMIN_TOTP_SECRET` | 管理后台二步验证密钥，见下方说明 |

生成随机密钥的命令：

```bash
openssl rand -hex 32
```

运行两次，分别填入 `SECRET_KEY` 和 `ENCRYPTION_KEY`。

### TOTP 配置

（待补充）

---

## 5. 启动服务

```bash
docker compose up -d --build
```

首次启动会自动完成以下操作：

- 构建镜像
- 启动 MongoDB
- 启动 Web/API 服务
- 启动定时任务调度器
- 导入座位信息到数据库

---

## 6. 验证服务状态

查看各容器运行状态：

```bash
docker compose ps
```

查看日志（按 Ctrl+C 退出）：

```bash
docker compose logs -f flask-api
docker compose logs -f scheduler
```

访问用户端页面：[http://localhost:5004](http://localhost:5004)

访问管理后台：[http://localhost:5004/admin](http://localhost:5004/admin)

---

## 常用运维命令

```bash
# 重启所有服务
docker compose restart

# 重启单个服务
docker compose restart flask-api

# 停止并删除容器（保留数据）
docker compose down

# 停止并删除容器及数据卷（慎用，数据会清空）
docker compose down -v

# 重新构建镜像
docker compose build --no-cache && docker compose up -d
```
