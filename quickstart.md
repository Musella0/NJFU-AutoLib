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
git clone https://github.com/Musella0/NJFU-AutoLib.git && cd NJFU-AutoLib
```

> 想让 AI 代劳？把 [DEPLOY_PROMPT.md](DEPLOY_PROMPT.md) 里的整段提示词丢给有终端权限的 AI 助手即可。

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
| `SESSION_COOKIE_SECURE` | 配了 HTTPS 填 `true`；纯 HTTP 访问填 `false`，否则登录后会立刻掉线 |
| `TRUSTED_PROXY_COUNT` | 反向代理层数。只有自带 Caddy 填 `1`（默认）；前面还有 CDN 填 `2` |

生成随机密钥的命令：

```bash
openssl rand -hex 32
```

运行两次，分别填入 `SECRET_KEY` 和 `ENCRYPTION_KEY`（两个值不要相同）。

> ⚠️ `ENCRYPTION_KEY` 用于加密用户的统一身份认证密码。**丢失或更改后，数据库里已存的密码全部无法解密**，所有用户都需要重新添加学号。请单独备份。

### 管理员账号与 TOTP

管理员不通过环境变量配置，而是在服务启动后用交互式脚本创建：

```bash
docker compose exec flask-api python init_admin.py
```

按提示输入账号、找回密码邮箱和密码，脚本会生成 TOTP 二步验证密钥。
**该密钥只显示一次，请立即添加到验证器 App（如 Google Authenticator）。**

---

## 5. 配置域名（对外访问必需）

编辑根目录的 `Caddyfile`，把第一行的两个域名换成你自己的：

```
http://你的域名, https://你的域名 {
```

Caddy 会在首次启动时自动申请 Let's Encrypt 证书，前提是：

- 域名的 A/AAAA 记录已指向本机公网 IP
- 80 / 443 端口未被 nginx 等占用，且云厂商安全组已放行

> 只在本机或内网试用、不需要域名的话，可以跳过这步，直接访问 `http://服务器IP:5004`。
> 但 `docker-compose.yml` 默认把 API 端口绑定在 `127.0.0.1`，需要改成 `0.0.0.0` 才能从别的机器访问——这会让服务**明文暴露**，请勿用于公网。

---

## 6. 启动服务

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

## 7. 验证服务状态

```bash
docker compose ps
```

`mongo` / `flask-api` / `scheduler` / `caddy` 应处于运行中；
`seed` 显示 `Exited (0)` 是**正常的**——它是一次性的座位导入任务，跑完即退出。

访问用户端页面：`https://你的域名/`（未配域名时为 [http://localhost:5004](http://localhost:5004)）

访问管理后台：`https://你的域名/admin`
