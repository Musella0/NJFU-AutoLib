# 分机部署手册

抢座程序搬到国内，公网入口留在境外。

```
Cloudflare (Flexible, 明文回源 :80)
    │
    ▼
edge  新加坡腾讯云 43.134.16.37 —— 只跑 Caddy：公网入口 + APK 下载
    │  WireGuard  10.8.0.1 ⇄ 10.8.0.2
    ▼
backend  宿迁京东云 —— mongo + flask-api + scheduler + 全部日志
    │
    ▼ 出站直连
webvpn.njfu.edu.cn
```

隧道只承载用户的网页/App 请求与响应，以及保活包。07:00 抢座、迟到保护、
邮件通知全在 backend 本地跑，一个字节都不过隧道——**隧道断了只是用户看到 502，
抢座照常**。

---

## 一、为什么这么分

2026-09-12 实测，同一条真实业务 URL（带 webvpn 编码路径的 `ic-web/login/publicKey`），
两边都返回 302，校方没有拦截京东云网段：

| | 宿迁 | 新加坡 | 倍数 |
|---|---|---|---|
| TCP connect | 0.019s | 0.260s | 13.7× |
| 完整请求 | 0.082s | 0.696s | 8.5× |
| 波动 | 0.082~0.103s | 0.572~2.008s | 稳定 vs 3.5 倍抖动 |

CAS 登录是多跳重定向，按 6~8 跳算，新加坡光 RTT 就 1.5~2s，宿迁 0.12~0.16s。

**数据库必须跟着调度器走**：07:00 开枪前要串行查库规划（活跃账号 + 每账号闭馆查询 +
每张备选座位 ID），二十来个账号约 100 次查询；本机几十毫秒，跨海每次 100~200ms
就是十几秒。开枪过程中每一发还要写 `reserve_attempts` 和 `user_config_info`。
API 再跟着库走。

代码零改动，只动了配置：

| 文件 | 改动 |
|---|---|
| `docker-compose.yml` | flask-api 端口绑定改 `${API_BIND:-127.0.0.1}`；caddy 多一个 `API_UPSTREAM` 环境变量 |
| `Caddyfile` | `reverse_proxy {$API_UPSTREAM:flask-api:5004}`，默认值不变 |
| `docker-compose.edge.yml` | 新增，edge 只起 Caddy 用 |
| `deploy/wireguard/` | 两端 wg0.conf 模板 |

单机部署的人什么都不用改，默认值就是原来的行为。

---

## 二、前置检查

**backend（宿迁）**

```bash
# 到 webvpn 的延迟，预期 connect 0.01~0.03s
WV='https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIwNS4xNTguMjAwLjE3MS4xNTMuMTUwLjIxNi45Ny4yMTEuMTU2LjE1OC4xNzMuMTQ4LjE1NS4xNTUuMjE3LjEwMC4xNTAuMTY1/ic-web/login/publicKey?vpn-12-libseat.njfu.edu.cn'
curl -sk -o /dev/null -w 'http=%{http_code} connect=%{time_connect} total=%{time_total}\n' "$WV"   # 预期 302

# 时钟。07:00:00 开枪，机器慢 150ms 就等于白搬
timedatectl status | grep -E 'synchronized|NTP'
chronyc tracking | grep -iE 'system time|last offset'

# 出境依赖
curl -s -o /dev/null -w 'resend=%{http_code} %{time_total}s\n'   https://api.resend.com/
curl -s -o /dev/null -w 'deepseek=%{http_code} %{time_total}s\n' https://api.deepseek.com/
```

> **Resend 从国内很慢**：实测 0.85~4.9s 且抖动大（DeepSeek 0.16s，无影响）。
> 通知是异步队列，不阻塞抢座，但切换后要盯发信失败率。真扛不住就让
> `utils/notify.py` 穿隧道回 edge 中转，或换国内服务商。

**内存**：backend 是 2C/2G。mongo 的 WiredTiger cache 会吃到 ~450MB，
加上两个 Python 进程和 8 并发抢座，**必须先加 swap**，否则 OOM 优先杀 mongo：

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/99-autolib-swap.conf && sysctl -p /etc/sysctl.d/99-autolib-swap.conf
```

---

## 三、建隧道

配置见 `deploy/wireguard/`。两边都是 `install -d -m 700 /etc/wireguard`
→ `(umask 077; wg genkey > …)` → 互填公钥 → `systemctl enable --now wg-quick@wg0`。

**腾讯云防火墙必须放行 UDP 51820 入站。** 主机防火墙干净也没用，包在云平台
那一层就被丢了。判断方法：在 edge 上 `tcpdump -ni any 'udp port 51820'`，
backend 每 25 秒一个保活包，抓不到就是防火墙。京东云侧不用开任何入站。

> edge 是**轻量应用服务器**，防火墙只有「允许」规则、没有拒绝也没有优先级。
> 09-12 排查时表里一条 UDP 规则都没有——添加规则的对话框协议默认是 TCP，
> 得手动改成 UDP。加完几秒内就握上手。

验证：

```bash
wg show                                  # latest handshake 要有值
ping -c3 10.8.0.1                        # 反向同理
ping -M do -s 1342 -c3 10.8.0.1          # 大包！wg0=1370 时 1342 是上限，1343 应失败
```

> 快速 ping（`-i 0.2`）会看到两成多丢包，但 RTT 标准差不到 0.5ms——那是 ICMP
> 限速，不是链路问题。09-14 实测 20 次 HTTP 全 200、2MB 文件一个字节不少。
> 拿 TCP 说话。

> MTU 不用手填：wg-quick 按出口网卡自动算，京东云 eth0 是 1450，减 80 得 1370，
> 实测精确。若哪天换了机器出现「ping 通、页面能开、返回体大的接口卡死」，
> 第一个查它。

**让 docker 等隧道起来**，否则重启后 flask-api 绑 `10.8.0.2:5004` 会失败：

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/after-wg.conf <<'EOF'
[Unit]
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
EOF
systemctl daemon-reload
```

配完**真重启一次验证**——`docker.socket` 有可能绕过 `docker.service` 的
`After=` 把 docker 提前拉起来，靠推理不如靠 `reboot`。

---

## 四、backend 准备（切换前随时可做）

```bash
git clone https://github.com/Musella0/NJFU-AutoLib autolib && cd autolib
```

`.env` 和 `data/` 不在版本库里，从 edge 取。**注意 `data/` 是 root 权限**，
直接 `scp -r` 会 Permission denied：

```bash
# 在 edge 上
cd ~/autolib && sudo tar czf /tmp/autolib-data.tgz data && sudo chown ubuntu /tmp/autolib-data.tgz
# 在 backend 上
scp edge:~/autolib/.env ./ && scp edge:/tmp/autolib-data.tgz /tmp/ && tar xzf /tmp/autolib-data.tgz
echo 'API_BIND=10.8.0.2' >> .env
docker compose build
```

**彩排一次导库**，别留到切换当晚第一次跑：完整走一遍下面第五节的 dump → scp →
restore，验证流程、量出耗时。当晚就变成重跑一个已知流程。

---

## 五、切换

**窗口：22:30 之后、次日 06:00 之前。** 22:00 闭馆后没有在跑的迟到保护；
06:45 快照、06:50 预登录、07:00 开抢都不能撞。

**两边调度器绝不能同时跑**，顺序固定。

> ### `.env` 不能 source
> 第 35 行 `RESEND_FROM=AutoLib <noreply@mail.musella.site>` 里的 `<` 会被 bash
> 当成重定向，`. ./.env` 直接报语法错误。下面统一用 sed 单独取值：
> ```bash
> MU=$(sed -n 's/^MONGO_USER=//p' .env); MP=$(sed -n 's/^MONGO_PASS=//p' .env)
> ```

**1. edge 停业务、导库**（用户从这里开始看到 502）

```bash
cd ~/autolib
docker compose stop scheduler flask-api
MU=$(sed -n 's/^MONGO_USER=//p' .env); MP=$(sed -n 's/^MONGO_PASS=//p' .env)
docker exec autolib-mongo mongodump --username "$MU" --password "$MP" \
  --authenticationDatabase admin --gzip --archive=/tmp/autolib.archive
docker cp autolib-mongo:/tmp/autolib.archive ./backups/autolib-$(date +%Y%m%d-%H%M%S).archive
scp ./backups/autolib-*.archive backend:~/autolib/backups/
# 记下条数，导入后对
docker exec autolib-mongo mongosh --quiet -u "$MU" -p "$MP" --authenticationDatabase admin AutoLib   --eval 'for (const c of ["users","user_config_info","devices","reserve_attempts","seat_snapshots","pending_segments"]) print(c, db[c].countDocuments())'
```

**2. backend 起库、导入、起全套**

```bash
cd ~/autolib
docker compose up -d mongo && sleep 15
MU=$(sed -n 's/^MONGO_USER=//p' .env); MP=$(sed -n 's/^MONGO_PASS=//p' .env)
docker cp backups/autolib-*.archive autolib-mongo:/tmp/autolib.archive
docker exec autolib-mongo mongorestore --username "$MU" --password "$MP" \
  --authenticationDatabase admin --gzip --archive=/tmp/autolib.archive --drop
# 核对条数，要和 edge 那边记下的一致（只看数量，不看内容）
docker exec autolib-mongo mongosh --quiet -u "$MU" -p "$MP" --authenticationDatabase admin AutoLib   --eval 'for (const c of ["users","user_config_info","devices","reserve_attempts","seat_snapshots","pending_segments"]) print(c, db[c].countDocuments())'
docker compose up -d mongo flask-api scheduler seed
docker logs autolib-scheduler --tail 20      # 要看到「定时预约调度器启动，每天 07:00 执行预约」
```

**3. edge 穿隧道探 API**

```bash
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' http://10.8.0.2:5004/   # 200/302，百毫秒级
```

**4. edge 换门卫**

```bash
cd ~/autolib
docker compose stop caddy
API_UPSTREAM=10.8.0.2:5004 docker compose -p autolib -f docker-compose.edge.yml up -d
```

会提示 orphan containers，只是警告，**不要加 `--remove-orphans`**（会把停着的
mongo/api/scheduler 删掉，回滚就没了）。

**5. 验证** — 浏览器登录、改一次明日时段再改回；App 打开一次；APK 下载仍 200；
backend `docker logs autolib-api` 能看到请求。确认无误后把 `API_UPSTREAM=10.8.0.2:5004`
写进 edge 的 `.env`。

**6. 次日 07:00 盯一眼**

```bash
docker logs autolib-scheduler --since 06:49 | grep -E '预登录|响应状态码|预约成功|预约失败'
```

首个请求耗时对比新加坡时期的 0.38~0.60s。

---

## 六、回滚

edge 整套还在，库是切换前那一刻的。越早回滚丢得越少；过夜再切要重新导库。

```bash
# backend
docker compose stop scheduler flask-api
# edge —— 别用 down，会删掉 project 共用的网络
docker compose -p autolib -f docker-compose.edge.yml rm -sf caddy
docker compose up -d
```

---

## 七、日常

| 事情 | 怎么做 |
|---|---|
| 改代码 | 本地改 → commit/push → `ssh backend 'cd autolib && git pull && docker compose up -d --build flask-api scheduler'` |
| 看日志 / 查库 / 备份 | `ssh backend` |
| 改 Caddyfile / 发 APK | edge：`API_UPSTREAM=10.8.0.2:5004 docker compose -p autolib -f docker-compose.edge.yml up -d` |
| 同步 `.env` / `data/` | 不在 git 里，改动少，手动 scp |
| 备份 | 改在 backend 做（第五节那条 `mongodump`），edge 上不再有库 |

- SSH 走隧道（`Host backend` / `HostName 10.8.0.2`），京东云 22 不用对公网开；
  控制台网页终端留作兜底，但建议安全组也放行自己常用 IP 的 22，网页终端贴长命令很痛苦。
- edge 的 `autolib-docker_mongo_data` 卷留一两周再清。
- `TRUSTED_PROXY_COUNT` **不用改**：链路仍是 CF → Caddy → flask-api 三段，
  Caddy 只是换了上游地址，没有多加一层。
- 隧道断了只有用户会告诉你。edge 上挂个一分钟一次的 curl 检查，成本很低。
