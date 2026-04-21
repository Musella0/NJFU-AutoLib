# AutoLib Quick Start

本教程按系统分类整理：

- Windows
- Linux

目前只编写 `Linux 服务器端部署教程`。  
Windows 和其他部分先留空，后续再补。

## Windows

待补充。

## Linux

本节面向完全不会 Docker 的用户。  
你只需要照着命令一步一步执行，不需要理解底层原理。

本教程默认你的 Linux 服务器是：

- Ubuntu 22.04
- Ubuntu 24.04
- Debian 12

如果你是其他 Linux 发行版，也建议优先换成 Ubuntu 再部署，最省事。

## Linux 服务器端部署教程

### 第 1 步：登录你的服务器

先用 SSH 登录你的 Linux 服务器。

如果你本地是 Windows，可以用：

- Xshell
- FinalShell
- MobaXterm
- Windows Terminal

如果你本地是 macOS 或 Linux，可以直接打开终端执行：

```bash
ssh root@你的服务器IP
```

如果你不是 `root` 用户，也可以用普通用户登录，但下面很多命令需要加 `sudo`。

## 第 2 步：安装 Docker

这一步最关键。  
下面是适合 Ubuntu / Debian 的完整安装步骤。

### 2.1 更新系统软件包

```bash
sudo apt update
sudo apt upgrade -y
```

### 2.2 安装基础依赖

```bash
sudo apt install -y ca-certificates curl gnupg
```

### 2.3 创建 Docker 密钥目录

```bash
sudo install -m 0755 -d /etc/apt/keyrings
```

### 2.4 下载 Docker 官方 GPG 密钥

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
```

设置权限：

```bash
sudo chmod a+r /etc/apt/keyrings/docker.gpg
```

### 2.5 添加 Docker 官方软件源

```bash
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

### 2.6 再次更新软件包索引

```bash
sudo apt update
```

### 2.7 安装 Docker 和 Compose 插件

```bash
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git openssl
```

这里顺手把下面会用到的工具也一起装了：

- `git`
- `openssl`

### 2.8 启动 Docker

```bash
sudo systemctl enable docker
sudo systemctl start docker
```

### 2.9 检查 Docker 是否安装成功

先看版本：

```bash
docker --version
docker compose version
```

如果你执行 `docker --version` 提示权限不足，可以先直接用：

```bash
sudo docker --version
sudo docker compose version
```

### 2.10 可选：让当前用户可以直接用 docker

如果你不想每次都输入 `sudo`，执行：

```bash
sudo usermod -aG docker $USER
```

执行后请退出当前 SSH，再重新登录一次。

重新登录后测试：

```bash
docker --version
docker compose version
```

如果还是不行，就继续在命令前面加 `sudo`，也完全可以正常使用。

## 第 3 步：从 GitHub 下载项目

先进入你准备存放项目的目录，比如：

```bash
cd /opt
```

然后从 GitHub 直接拉取项目：

```bash
git clone git@github.com:Musella0/autolib.git
```

如果你的服务器没有配置 GitHub SSH Key，也可以使用 HTTPS 方式：

```bash
git clone https://github.com/Musella0/autolib.git
```

下载完成后进入项目目录：

```bash
cd autolib
```

你可以执行下面命令确认项目已下载成功：

```bash
ls
```

正常应该能看到：

- `docker-compose.yml`
- `.env.example`
- `backend`
- `README.md`

## 第 4 步：创建环境配置文件

执行：

```bash
cp .env.example .env
```

这一步是在生成真正给项目使用的配置文件。

## 第 5 步：生成必须填写的密钥

这个项目至少需要两个密钥：

- `SECRET_KEY`
- `ENCRYPTION_KEY`

你可以用下面命令生成：

```bash
openssl rand -hex 32
```

执行两次，得到两串不同的随机字符串。

例如：

```text
6f2fxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
9a8bxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

然后：

- 第一串填到 `SECRET_KEY`
- 第二串填到 `ENCRYPTION_KEY`

### 可选：生成管理员后台 TOTP 密钥

如果你还要使用管理后台 `/admin`，还需要生成一个动态验证码密钥。

执行：

```bash
python3 - <<'PY'
import base64, os
print(base64.b32encode(os.urandom(20)).decode().rstrip('='))
PY
```

如果你的服务器没有 `python3`，先安装：

```bash
sudo apt install -y python3
```

把输出结果填到 `ADMIN_TOTP_SECRET`。

如果你暂时不用管理后台，这一项可以先留空。

## 第 6 步：编辑 `.env` 配置文件

打开配置文件：

```bash
nano .env
```

至少要改下面这几项：

```env
MONGO_PASS=改成你自己的数据库密码
SECRET_KEY=填入你刚才生成的第一串随机字符串
ENCRYPTION_KEY=填入你刚才生成的第二串随机字符串
ADMIN_TOTP_SECRET=如果要用管理后台，就填这里
```

### 推荐的最小配置示例

你可以参考下面这样填写：

```env
MONGO_USER=autolib
MONGO_PASS=Autolib123456
API_PORT=5004
SECRET_KEY=这里替换成你生成的SECRET_KEY
SESSION_COOKIE_SECURE=false
ADMIN_TOTP_SECRET=这里替换成你的TOTP密钥
ENCRYPTION_KEY=这里替换成你生成的ENCRYPTION_KEY
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=00
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=
SMTP_PASS=
```

### 这些配置是什么意思

只需要理解最关键的几个：

- `MONGO_PASS`：数据库密码，自己改掉就行
- `API_PORT`：网页访问端口，默认是 `5004`
- `SECRET_KEY`：网站登录和会话安全用的密钥
- `ENCRYPTION_KEY`：加密保存账号密码用的密钥
- `ADMIN_TOTP_SECRET`：管理后台动态验证码密钥
- `SCHEDULE_HOUR` 和 `SCHEDULE_MINUTE`：每天自动预约的时间

### 保存并退出

如果你用的是 `nano`：

1. 按 `Ctrl + O`
2. 按回车
3. 按 `Ctrl + X`

## 第 7 步：启动项目

在项目目录执行：

```bash
docker compose up -d --build
```

如果你当前用户还不能直接用 Docker，就执行：

```bash
sudo docker compose up -d --build
```

第一次启动时，系统会自动做这些事：

- 下载依赖镜像
- 构建项目镜像
- 启动数据库
- 启动网页服务
- 启动定时任务
- 初始化座位数据

第一次可能会稍微慢一点，请耐心等。

## 第 8 步：检查项目是否启动成功

### 查看容器状态

```bash
docker compose ps
```

如果你使用的是 `sudo`：

```bash
sudo docker compose ps
```

如果你看到类似 `Up`、`running`、`healthy` 这样的状态，说明项目基本已经启动成功。

### 查看日志

查看全部日志：

```bash
docker compose logs -f
```

只看网页服务日志：

```bash
docker compose logs -f flask-api
```

只看定时任务日志：

```bash
docker compose logs -f scheduler
```

退出日志界面按：

```text
Ctrl + C
```

## 第 9 步：开放服务器端口

如果你部署在云服务器，浏览器打不开页面时，最常见原因不是项目没启动，而是端口没开放。

这个项目默认使用：

```text
5004
```

你需要检查两层地方：

### 9.1 Linux 防火墙

如果你的服务器启用了 `ufw`，执行：

```bash
sudo ufw allow 5004/tcp
sudo ufw reload
```

### 9.2 云服务器安全组

如果你用的是：

- 阿里云
- 腾讯云
- 华为云
- Oracle Cloud
- AWS

还要去对应云平台的控制台里，放行端口 `5004`。

这一步非常重要。  
很多人项目已经启动成功，但就是因为没开安全组，导致浏览器打不开。

## 第 10 步：打开网页

如果你是在本机 Linux 上部署，打开：

```text
http://localhost:5004/
```

如果你是服务器部署，打开：

```text
http://你的服务器IP:5004/
```

例如：

```text
http://123.123.123.123:5004/
```

管理后台地址是：

```text
http://你的服务器IP:5004/admin
```

## 第 11 步：开始使用

打开网页后，建议按下面顺序操作：

### 11.1 注册网站账号

虽然支持游客模式，但更建议注册，这样方便长期保存配置。

### 11.2 添加学号

添加你自己的学号。

### 11.3 填写两个密码

需要填写：

- VPN 密码
- 图书馆密码

这两个密码不是同一个，别填错。

### 11.4 选择座位优先级

把你想预约的座位按顺序加入列表。  
系统会优先尝试排在前面的座位。

### 11.5 配置时间

你可以选择：

- 按星期分别设置
- 或者统一设置每天时段

### 11.6 验证密码

先验证一次，确保：

- VPN 密码正确
- 图书馆密码正确

### 11.7 保存配置

验证通过后再保存。

### 11.8 手动测试一次

建议第一次配置完成后，立即手动测试一次预约。  
这样能最快确认你的配置有没有问题。

## 常用命令

### 启动项目

```bash
docker compose up -d --build
```

### 查看运行状态

```bash
docker compose ps
```

### 查看全部日志

```bash
docker compose logs -f
```

### 只看网页服务日志

```bash
docker compose logs -f flask-api
```

### 只看定时任务日志

```bash
docker compose logs -f scheduler
```

### 重启项目

```bash
docker compose restart
```

### 停止项目

```bash
docker compose down
```

### 重新导入座位数据

```bash
docker compose run --rm seed
```

## 常见问题

### 1. `docker: command not found`

说明 Docker 还没装好，回到前面的 Docker 安装步骤重新执行。

### 2. `docker compose: command not found`

说明 Compose 插件没装好。  
请确认你安装的是：

```bash
sudo apt install -y docker-compose-plugin
```

### 3. 页面打不开

按顺序检查：

1. 项目是否启动成功：`docker compose ps`
2. 日志里是否报错：`docker compose logs -f`
3. 端口 `5004` 是否开放
4. 云服务器安全组是否放行

### 4. 改完 `.env` 以后没生效

执行：

```bash
docker compose up -d --build
```

或者至少执行：

```bash
docker compose restart
```

### 5. 管理后台登录不了

重点检查：

- `.env` 里有没有填写 `ADMIN_TOTP_SECRET`
- 手机验证器绑定的密钥对不对
- 服务器时间准不准

动态验证码对时间非常敏感。

## 其他部分

### Windows 服务器端

待补充。

### Windows 客户端连接 Linux 服务器

待补充。

### Linux 桌面本地部署

待补充。

### 反向代理与 HTTPS

待补充。
