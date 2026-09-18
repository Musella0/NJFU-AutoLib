# 让 AI 帮你部署

把下面整段复制给有终端权限的 AI 助手（Claude Code / Cursor / Codex 等），
在服务器上运行，或让它能 SSH 到服务器。

---

请帮我把 AutoLib 部署到我自己的服务器上。仓库：https://github.com/Musella0/NJFU-AutoLib
这是一个图书馆自动抢座系统，Flask + MongoDB + Caddy，用 Docker Compose 编排。
先克隆下来读 `quickstart.md`、`docker-compose.yml`、`.env.example` 和 `Caddyfile`，
按你理解的正确顺序执行，每步验证结果再继续，失败就停下来告诉我原因。

动手前先问我四件事：**域名**（要提前解析到这台服务器，Caddy 靠它自动签 HTTPS 证书）、
**服务器前面是否还有 CDN 或另一层反向代理**、**是否需要邮件通知**（需要的话我给你 Resend 或 SMTP 配置）、
**是否需要配置 Android 客户端**（需要的话我只提供自己的服务器地址，改哪里、怎么编译你自己判断）。

`quickstart.md` 和 `.env.example` 里凡是带 ⚠️ 或写了「否则……」的地方都是踩过的坑
（反代层数、加密密钥不可丢、Cookie 与协议要匹配、一次性容器正常退出等），逐条照做别跳过。
`ENCRYPTION_KEY` 生成后额外提醒我单独备份。

第四问我若回答「需要」，就用我给的地址替换掉客户端里写死的服务端常量并编译出可安装的包，
中间不用再问我细节。只提示两点：客户端强制 HTTPS，自签证书要额外配置信任；
release 签名口令来自不在版本库里的 `android/local.properties`，缺失时产出的是无法直接安装的未签名包。

过程中不要把密钥、密码打印到终端输出里，写进文件即可；用中文跟我交流。
部署完提醒我：这个项目会代替用户登录学校账号并自动抢座，需自行确认符合学校规定，
并对存储他人凭据承担责任。
