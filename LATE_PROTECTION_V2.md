# 迟到保护改造 v2：用「在馆探针」做否决闸

> 结论来源：2026-09-15 实测，34 个账号全量对照，零反例。
> 背景见 `LATE_PROTECTION_PLAN.md`（那份里「服务端拿不到在馆信号，只能预测」的判断已被本文推翻）。

---

## 一、核心发现

```
GET  {base_url}ic-web/phoneSeatReserve/duration{vpn_suffix}
headers: { "token": <从 ic-cookie 里抠出的 token>, "lan": "1" }
无需任何参数
```

| 人的状态 | 返回 |
|---|---|
| 不在馆 | `code=1`，`message="用户需要在馆状态才能操作"` |
| 在馆 | `code=1`，`message="设备在该时间段内已被预约"`（过了在馆检查，撞到下一道校验） |

**必须带 `token` 请求头**——只靠 cookie 会认证失败。token 在 `ic-cookie` 的 `token=` 段里。

### 为什么确定它查的是人不是预约

34 账号列联表：

```
resvStatus=1027（待签到） → 不在馆  14/14
resvStatus=1093（使用中） → 在馆     9/9
无预约                    → 不在馆  10 / 在馆 1   ← 决定性
```

最后一行那个账号当天**没有任何预约**，接口仍判定「在馆」。若判断是从预约推的，这不可能发生。

### 为什么这解决了老问题

老闸门 2 查 `resvStatus`，但预约要到 `begin−1.2min` 才生效，在那之前恒为 1027，
所以 `begin−7min` 的检查结构性空转（实测一个月 0 次命中）。
在馆探针不依赖预约生效，**任意时刻都能问**，因此可以在 `begin−5min` 就判出来，
不必去挤 `begin−1.2min → begin` 那 72 秒。

---

## 二、要改的地方

### 1. `backend/utils/library_system.py` — 新增探针

```python
IN_LIBRARY_MESSAGES = {"设备在该时间段内已被预约"}
NOT_IN_LIBRARY_MESSAGES = {"用户需要在馆状态才能操作"}

def _ic_token(self) -> Optional[str]:
    """从 ic-cookie 里抠 token，扫码那套接口要放在请求头里。"""

def is_in_library(self) -> Optional[bool]:
    """人此刻在不在馆。True=在馆，False=不在馆，None=判不出来（文案变了/网络错）。"""
```

**判据必须是白名单，且未知一律当「不在馆」。**
失败方向：若图书馆改文案而我们按「不等于那句就是在馆」来判，会误判在馆 → 放行保护 → 用户违约扣分。
反过来未知当不在馆，最坏只是退回今天的行为，不会坑人。遇到未知文案要 `log.warning` 告警。

### 2. `backend/scheduled_task.py:1874` 附近 — 加否决闸

现有闸门 2（查 `resvStatus`，命中 `IN_LIBRARY_STATUSES` 则跳过）**保留**，
在它之前或之后加在馆探针：

```
in_lib = library.is_in_library()
if in_lib is True:
    log「在馆探针：用户已在馆，跳过迟到保护」
    _record_visit_log(...)
    return
# False 或 None 都继续走原流程
```

### 3. `backend/scheduled_task.py:2020` — 改触发时间

```python
exec_time = begin_time - timedelta(minutes=7)   # 旧
exec_time = begin_time - timedelta(minutes=5)   # 新
```

建议抽成常量 / 环境变量 `LATE_PROTECT_LEAD_MINUTES`，默认 5。

---

## 三、注意事项

- `phoneSeatReserve/` 下面 `sign / tempLeave / quit / reserve / end / comeback / signLogin` **都是写操作**，排查时不要碰。只读的是 `duration`(get)、`pendingSign`(get)、`valid`(get，参数未知)。
- `pendingSign` 在馆/不在馆都返回 `code=0`，**不能**用来判断。
- `auth/webapp` 恒「认证失败」；`login/user` 密码登录被学校关了（`code=301`），CAS 模式 `code=900`——都不是这条路。
- 预约生效固定在 `begin−1.2min`，人已在馆的会在那一秒被系统自动签到（`consoleKind=1 系统`）；
  人没到的等本人刷闸机（`consoleKind=8`）。见 memory `checkin-auto-at-activation`。
- 仍未验证：`begin−1.2min` 生效之后到 `begin` 之间还能不能取消预约。
  改成 `begin−5min` 之后这个问题不再挡路，但如果将来想再往后挪，得先测它。

---

## 四、预期收益

2026-09-15 15:0x 的横截面：34 人里 9 人在馆。这些人现在全被无差别推后一小时。
`arrival_checks` 历史 633 条里 `violated` 占 29.5%（有结论的 466 条里占 40%），
所以保护本身有用，要砍掉的只是对已到馆者的误伤。
