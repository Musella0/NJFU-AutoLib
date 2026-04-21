# AutoLib 小白部署教程

这是一份给完全不懂 Docker 的使用说明。

目标只有两件事：

1. 把项目跑起来
2. 正常打开网页开始使用

这份文档不讲原理，不讲架构，只讲照着做。

## 你需要准备什么

开始之前，请先确认你已经有下面这些东西：

- 一台服务器或者电脑
- 已经安装好 Docker
- 已经安装好 Docker Compose
- 已经把这个项目文件夹上传到机器上

如果你已经能在终端里执行下面两个命令，就说明环境基本没问题：

```bash
docker --version
docker compose version
```

## 第一步：进入项目目录

假设你已经把项目上传到了服务器上：

```bash
cd /你的项目路径/autolib
```

比如你的项目就在当前目录，那就是：

```bash
cd autolib
```

进入后，你可以先看一下文件是否齐全：

```bash
ls
```

正常应该能看到这些文件或目录：

- `docker-compose.yml`
- `.env.example`
- `backend`

## 第二步：复制配置文件

执行下面这条命令：

```bash
cp .env.example .env
```

这一步的意思很简单：复制出一份真正要使用的配置文件。

## 第三步：生成必须填写的密钥

这一步不要跳过。

先生成两个随机密钥：

```bash
openssl rand -hex 32
```

这条命令执行两次。

你会得到两串很长的字符，像这样：

```text
e3f7......
9ab2......
```

把第一串留给 `SECRET_KEY`，第二串留给 `ENCRYPTION_KEY`。

如果你还想使用管理后台 `/admin`，再生成一个管理员验证码密钥：

```bash
docker run --rm python:3.11-slim python -c "import base64, os; print(base64.b32encode(os.urandom(20)).decode().rstrip('='))"
```

把输出结果留给 `ADMIN_TOTP_SECRET`。

如果你暂时不用管理后台，这一项可以先不管。

## 第四步：编辑 `.env`

用你熟悉的编辑器打开：

```bash
nano .env
```

你至少要改这几项：

```env
MONGO_PASS=改成你自己的强密码
SECRET_KEY=填入你刚才生成的第一串
ENCRYPTION_KEY=填入你刚才生成的第二串
ADMIN_TOTP_SECRET=如果要用管理后台，就填入刚才生成的 base32 字符串
```

如果你只是自己用，下面这些通常可以先保持默认：

```env
MONGO_USER=autolib
API_PORT=5004
SESSION_COOKIE_SECURE=false
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=00
```

### 一个可直接参考的最小示例

你可以把 `.env` 改成类似这样：

```env
MONGO_USER=autolib
MONGO_PASS=Autolib123456
API_PORT=5004
SECRET_KEY=这里替换成你生成的32字节hex字符串
SESSION_COOKIE_SECURE=false
ADMIN_TOTP_SECRET=这里替换成你的TOTP密钥
ENCRYPTION_KEY=这里替换成你生成的32字节hex字符串
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=00
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=
SMTP_PASS=
```

### 保存并退出 `nano`

如果你用的是 `nano`：

1. 按 `Ctrl + O`
2. 按回车
3. 按 `Ctrl + X`

## 第五步：启动项目

执行：

```bash
docker compose up -d --build
```

第一次启动会稍微慢一点，因为它要自动做这些事：

- 下载基础镜像
- 安装依赖
- 启动数据库
- 启动网页服务
- 启动定时任务
- 初始化座位数据

如果中间没有报错，就说明基本启动成功了。

## 第六步：检查是否启动成功

先看服务状态：

```bash
docker compose ps
```

如果你看到类似 `Up`、`running` 这样的状态，说明服务已经起来了。

再看日志：

```bash
docker compose logs -f
```

如果日志太多，你可以只看网页服务：

```bash
docker compose logs -f flask-api
```

只看定时任务：

```bash
docker compose logs -f scheduler
```

退出日志界面按：

```text
Ctrl + C
```

## 第七步：打开网页

如果你是在本机部署，直接打开浏览器访问：

```text
http://localhost:5004/
```

如果你是部署在服务器上，把 `localhost` 换成你的服务器 IP：

```text
http://你的服务器IP:5004/
```

比如：

```text
http://123.123.123.123:5004/
```

管理后台地址是：

```text
http://你的服务器IP:5004/admin
```

## 第八步：第一次使用怎么操作

打开用户端首页后，建议按下面顺序来。

### 1. 注册或直接使用

这个项目支持游客模式，但建议直接注册一个 Web 账号，方便长期保存多个学号配置。

### 2. 添加学号

进入页面后，添加你的学号。

### 3. 填写两个密码

你需要填写：

- VPN 密码
- 图书馆密码

注意，这两个密码不是一回事，不要填混。

### 4. 选择座位优先级

把你希望抢的座位按顺序加入列表。

排在前面的座位会优先尝试。

### 5. 配置预约时间

你可以按自己的使用习惯设置：

- 按星期分别设置
- 或者统一时段设置

### 6. 验证密码

保存前，先做一次验证，确认：

- VPN 密码正确
- 图书馆密码正确

### 7. 保存配置

验证通过后保存。

### 8. 立即试一次

建议第一次保存后，手动点一次“立即预约”或“立即抢一次”，这样你能马上知道配置是否正常。

## 第九步：如果要使用管理后台

管理后台地址：

```text
http://你的服务器IP:5004/admin
```

登录方式不是普通密码，而是动态验证码。

你需要：

1. 在 `.env` 里提前配置好 `ADMIN_TOTP_SECRET`
2. 用手机安装一个验证器 App
3. 常见 App：
   - Google Authenticator
   - Microsoft Authenticator
   - Aegis
4. 登录后台后，根据页面提示绑定二维码
5. 以后用 6 位动态码登录

如果你没有配置 `ADMIN_TOTP_SECRET`，管理后台通常无法正常登录。

## 常用命令

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

### 重启全部服务

```bash
docker compose restart
```

### 只重启网页服务

```bash
docker compose restart flask-api
```

### 只重启定时任务

```bash
docker compose restart scheduler
```

### 重新导入座位数据

```bash
docker compose run --rm seed
```

### 停止项目

```bash
docker compose down
```

### 停止并删除数据

这条命令会删除数据库数据，慎用：

```bash
docker compose down -v
```

## 最常见的几个问题

### 1. 浏览器打不开页面

按顺序检查：

- 服务是否真的启动了：`docker compose ps`
- 端口是不是 `5004`
- 服务器防火墙有没有放行 `5004`
- 云服务器安全组有没有开放 `5004`

如果你是阿里云、腾讯云、华为云这类服务器，这一步很常见。

### 2. 启动时报错

先看日志：

```bash
docker compose logs -f
```

最常见原因：

- `.env` 没填好
- 密钥为空
- 端口被占用
- Docker 没安装好
- 网络太慢导致拉镜像失败

### 3. 改了 `.env` 但是没生效

改完配置后，执行：

```bash
docker compose up -d --build
```

或者至少重启：

```bash
docker compose restart
```

### 4. 管理后台登录不了

先检查：

- `.env` 里是否填写了 `ADMIN_TOTP_SECRET`
- 手机验证器里的密钥是否绑对
- 服务器时间是否正确

动态验证码对时间很敏感，服务器时间不准时容易失败。

### 5. 想换端口

修改 `.env` 里的：

```env
API_PORT=5004
```

比如改成：

```env
API_PORT=8080
```

然后重启：

```bash
docker compose up -d --build
```

以后访问地址就变成：

```text
http://你的服务器IP:8080/
```

## 推荐的实际使用顺序

如果你是第一次部署，最省事的顺序就是下面这样：

1. `cp .env.example .env`
2. 生成 `SECRET_KEY`
3. 生成 `ENCRYPTION_KEY`
4. 编辑 `.env`
5. `docker compose up -d --build`
6. `docker compose ps`
7. 打开 `http://你的IP:5004/`
8. 添加学号
9. 填密码
10. 验证密码
11. 保存配置
12. 手动试一次预约

## 最后提醒

请至少记住这三点：

1. `.env` 很重要，不要乱删
2. `ENCRYPTION_KEY` 很重要，改丢了可能导致之前保存的密码无法正常使用
3. 如果你只是普通使用者，其实只需要会这几个命令：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

如果你后面还想要，我可以继续把这份 `quickstart.md` 再压缩成一版“5分钟部署版”，只保留最少步骤。
