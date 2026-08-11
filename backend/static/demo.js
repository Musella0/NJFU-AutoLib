// ============ AutoLib · 前端演示模式 ============
// 用 ?demo=1 打开，页面内伪造一份完整的账号数据，用于截图预览。
//
// 只在浏览器里生效：所有 /api 请求在 fetch 层被拦下，一个字节都不会发到后端，
// 写操作（预约/取消/到馆/保存）一律拒绝，所以拿生产站点开演示也不会动到真实数据。
//
//   开启：  ?demo=1     （同标签页内刷新保持，换页也还在）
//   关闭：  ?demo=0     （或直接关掉标签页）
//
// 每次刷新重新随机一份座位、时段和学习记录，多刷几次挑好看的截图。

(function () {
  const KEY = 'autolib_demo';
  const params = new URLSearchParams(location.search);
  if (params.has('demo')) {
    const v = params.get('demo');
    if (v !== '0' && v !== 'false') sessionStorage.setItem(KEY, '1');
    else sessionStorage.removeItem(KEY);
  }
  if (sessionStorage.getItem(KEY) !== '1') return;

  // 欢迎弹窗会挡住主页，演示时默认跳过；要截欢迎图就用 ?demo=1&welcome=1。
  // 走全局标志而不是写 localStorage，免得把真实用户的「已读」状态污染掉。
  window.__autolibSkipWelcome = params.get('welcome') !== '1';

  // ---------- 演示账号 ----------
  const PID = '1234567';
  const NICKNAME = '';          // 留空则顶部显示学号；填了就显示昵称
  const EMAIL = 'demo@njfu.edu.cn';

  // ---------- 随机工具 ----------
  const rnd = (a, b) => a + Math.floor(Math.random() * (b - a + 1));
  const pick = (arr) => arr[rnd(0, arr.length - 1)];
  const chance = (p) => Math.random() < p;

  const ZONES = ['3F', '4F', '5F'];
  const ROWS = 'ABCD';

  function randomSeat() {
    return `${pick(ZONES)}-${pick(ROWS)}${String(rnd(1, 60)).padStart(3, '0')}`;
  }

  function seatCatalog() {
    const seats = {};
    ZONES.forEach((z) => {
      const list = [];
      for (const r of ROWS) {
        for (let i = 1; i <= 20; i++) list.push(`${z}-${r}${String(i).padStart(3, '0')}`);
      }
      seats[z] = list;
    });
    return seats;
  }

  // 周五 20:00 闭馆，结束时间要跟着收
  function randomRange(isoDay) {
    const start = pick(['08:00', '08:30', '09:00', '10:00', '13:00']);
    const cap = isoDay === 5 ? '20:00' : '22:00';
    const ends = ['17:00', '18:30', '20:00', '21:00', '22:00'].filter((e) => e > start && e <= cap);
    return `${start}-${pick(ends.length ? ends : ['20:00'])}`;
  }

  const pad = (n) => String(n).padStart(2, '0');
  const dateKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const isoOf = (d) => (d.getDay() === 0 ? 7 : d.getDay());

  function offsetDate(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return d;
  }

  // ---------- 生成这一次刷新的数据 ----------
  const today = new Date();
  const tomorrow = offsetDate(1);

  const seatList = (() => {
    const s = new Set();
    while (s.size < 3) s.add(randomSeat());
    return [...s];
  })();

  const todaySeat = pick(seatList);
  const tomorrowSeat = pick(seatList);
  const todayRange = randomRange(isoOf(today));
  const tomorrowRange = randomRange(isoOf(tomorrow));

  // 每周配置：随机排，挑一天休息，周五自动收到 20:00
  const weekTime = (() => {
    const w = {};
    const restDay = String(rnd(6, 7));       // 周末里随机休一天
    for (let i = 1; i <= 7; i++) {
      const k = String(i);
      w[k] = k === restDay ? [] : [randomRange(i)];
    }
    return w;
  })();

  function reservation(date, seat, range, status) {
    const [s, e] = range.split('-');
    const day = dateKey(date);
    return {
      uuid: `demo-${day}`,
      resvBeginTime: `${day} ${s}:00`,
      resvEndTime: `${day} ${e}:00`,
      resvStatus: status,
      devInfo: { devName: seat },
    };
  }

  // 今日「使用中」，明日「已预约」——卡片信息最全，截图最好看
  const reservations = [
    reservation(today, todaySeat, todayRange, 1093),
    reservation(tomorrow, tomorrowSeat, tomorrowRange, 1027),
  ];

  // 学习记录：本学期起点到今天，约六成日子有记录
  const visitStats = (() => {
    const start = new Date(today.getFullYear(), today.getMonth() < 6 ? 0 : 6, 1);
    const monday = (() => {
      const x = new Date(today);
      x.setDate(x.getDate() - (isoOf(x) - 1));
      x.setHours(0, 0, 0, 0);
      return x;
    })();

    const daily = [];
    let totalVisits = 0, totalMinutes = 0, weekVisits = 0, weekMinutes = 0;

    for (const d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
      const iso = isoOf(d);
      // 周末去得少一些，看起来更像真人的作息
      if (!chance(iso >= 6 ? 0.35 : 0.7)) continue;
      const visits = chance(0.25) ? 2 : 1;
      const minutes = rnd(70, 590);
      daily.push({ date: dateKey(d), visits, minutes });
      totalVisits += visits;
      totalMinutes += minutes;
      if (d >= monday) { weekVisits += visits; weekMinutes += minutes; }
    }

    return {
      total_visits: totalVisits,
      total_minutes: totalMinutes,
      this_week_visits: weekVisits,
      this_week_minutes: weekMinutes,
      daily,
      recent: [],
    };
  })();

  const accountDetail = {
    pid: PID,
    verified: true,
    is_reserved: 'True',
    late_protection: 'True',
    mode: 'week_time',
    time: { week_time: weekTime, tomorrow: '' },
    seat_list: seatList,
    notify_email: EMAIL,
    vpn_password: '',
    arrived_date: dateKey(today),
  };

  const napConfig = {
    start_time: '14:00',
    end_time: '',
    seat: '',
    auto_daily: true,
    trigger_time: '12:00',
  };

  const announcements = [
    {
      title: '演示数据',
      content: '当前是演示模式，页面上的座位、时段和学习记录都是随机生成的，不会连到学校系统。',
      level: 'info',
      pinned: true,
      updated_at: dateKey(today),
    },
    {
      title: '周五闭馆时间',
      content: '周五图书馆 20:00 关门，预约结束时间超过 20:00 的会自动截断。',
      level: 'warning',
      pinned: false,
      updated_at: dateKey(offsetDate(-3)),
    },
  ];

  const reservationResults = [
    {
      pid: PID,
      success: true,
      result: `已为你抢到 ${todaySeat}，${todayRange.replace('-', ' — ')}`,
      updated_at: `${dateKey(today)} 07:05`,
    },
    {
      pid: PID,
      success: true,
      result: `已为你抢到 ${tomorrowSeat}，${tomorrowRange.replace('-', ' — ')}`,
      updated_at: `${dateKey(offsetDate(-1))} 07:05`,
    },
  ];

  // ---------- 路由表 ----------
  const REFUSED = { success: false, message: '演示模式：不会真的执行操作', error: '演示模式：不会真的执行操作' };

  const ROUTES = [
    [/^\/api\/auth\/me$/, () => ({ logged_in: true, uid: PID, nickname: NICKNAME })],
    [/^\/api\/my\/accounts$/, () => [{ pid: PID, is_reserved: 'True', verified: true }]],
    [/^\/api\/my\/accounts\/[^/]+$/, () => accountDetail],
    [/^\/api\/my\/accounts\/[^/]+\/reservations$/, () => ({ reservations })],
    [/^\/api\/my\/accounts\/[^/]+\/nap_config$/, () => napConfig],
    [/^\/api\/my\/visit_stats$/, () => visitStats],
    [/^\/api\/seats$/, () => ({ seats: seatCatalog() })],
    [/^\/api\/announcements$/, () => announcements],
    [/^\/api\/my\/reservation_results$/, () => reservationResults],
  ];

  function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // ---------- 拦截 ----------
  const realFetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    let path;
    try { path = new URL(url, location.origin).pathname; } catch (e) { path = url; }

    if (!path.startsWith('/api/')) return realFetch(input, init);

    const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET') return Promise.resolve(jsonResponse(REFUSED));

    const hit = ROUTES.find(([re]) => re.test(path));
    if (!hit) return Promise.resolve(jsonResponse({}, 404));
    return Promise.resolve(jsonResponse(hit[1]()));
  };

  console.log(
    `%c AutoLib 演示模式 %c 学号 ${PID} · 今日 ${todaySeat} ${todayRange} · 明日 ${tomorrowSeat} ${tomorrowRange}\n` +
    '数据为随机生成，刷新可换一批；?demo=0 退出。',
    'background:#4f46e5;color:#fff;border-radius:3px;padding:2px 6px',
    'color:inherit'
  );
})();
