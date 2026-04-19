// ============ AutoLib · 手绘风 UI · 前端逻辑 ============

const state = {
  page: 'home',
  tmrOpen: false,
  noticesCollapsed: false,
  isGuest: true,
  uid: '',
  accounts: [],       // list of account summaries
  currentPid: '',     // currently selected pid
  currentCfg: null,   // full detail for currentPid
  allSeats: {},       // { zone: [seatName, ...] }
  todayResv: null,    // today's live reservation (if any)
  tomorrowResv: null, // tomorrow's live reservation (if any)
  cfgMode: 'week',    // UI: 'week' or 'simple'
  authMode: 'login',
};

const WEEK_LABELS = ['一','二','三','四','五','六','日']; // iso 1..7
const WEEK_DEFAULTS = ['08:00-22:00','08:00-22:00','08:00-22:00','08:00-22:00','08:00-20:00','08:00-22:00','08:00-22:00'];
const TIME_MIN = '08:00';
const TIME_MAX_DEFAULT = '22:00';
const TIME_MAX_FRIDAY = '20:00';

// iso weekday 1..7 (Fri=5). Pass null/undefined for generic 08-22 range.
function timeBounds(isoDay){
  const max = isoDay === 5 ? TIME_MAX_FRIDAY : TIME_MAX_DEFAULT;
  return { min: TIME_MIN, max };
}
function clampTime(t, min, max){
  if(!t) return min;
  if(t < min) return min;
  if(t > max) return max;
  return t;
}
function clampRange(s, e, isoDay){
  const { min, max } = timeBounds(isoDay);
  let cs = clampTime(s, min, max);
  let ce = clampTime(e, min, max);
  if(cs >= ce){ cs = min; ce = max; }
  return [cs, ce];
}

// 将 time 配置值规范为段字符串数组 ["HH:MM-HH:MM", ...]
// - 字符串 "HH:MM-HH:MM" → 单段数组
// - 数组 → 过滤掉无效/休息项
// - "休息" / "off" / 空 → []
function toSegments(raw){
  if(!raw) return [];
  if(Array.isArray(raw)){
    return raw.filter(x => typeof x === 'string' && x !== '休息' && x !== 'off' && /^\d\d:\d\d-\d\d:\d\d$/.test(x));
  }
  if(typeof raw === 'string'){
    if(raw === '休息' || raw === 'off') return [];
    if(/^\d\d:\d\d-\d\d:\d\d$/.test(raw)) return [raw];
  }
  return [];
}
// 周五 20:00 关闭
function friCap(seg){
  const [s, e] = seg.split('-');
  if(e && e > '20:00') return `${s}-20:00`;
  return seg;
}

// ---------- helpers ----------
function $(id){ return document.getElementById(id); }
function escHtml(s){ return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function toast(msg, type='info'){
  const box = $('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type==='success'?'ok':type==='error'?'err':'');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; setTimeout(()=>el.remove(), 300); }, 2800);
}

async function api(path, opts={}){
  const options = Object.assign({}, opts);
  if(options.body && typeof options.body === 'object'){
    options.headers = Object.assign({'Content-Type':'application/json'}, options.headers || {});
    options.body = JSON.stringify(options.body);
  }
  const r = await fetch(path, options);
  let data = null;
  try { data = await r.json(); } catch(e){}
  return { ok: r.ok, status: r.status, data: data || {} };
}

// ---------- navigation ----------
function go(p){
  state.page = p;
  document.querySelectorAll('.page').forEach(x => x.classList.remove('show'));
  $('page-'+p).classList.add('show');
  document.querySelectorAll('.bnav > button').forEach(b => b.classList.toggle('on', b.dataset.tab === p));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ---------- auth ----------
async function checkAuth(){
  try{
    const { data } = await api('/api/auth/me');
    if(data.logged_in){
      state.isGuest = false;
      state.uid = data.uid;
    }else{
      state.isGuest = true;
      state.uid = data.uid || '';
    }
  }catch(e){
    state.isGuest = true;
  }
  updateAuthUI();
}

function updateAuthUI(){
  const hello = $('hello');
  const dt = new Date();
  const wd = ['SUN','MON','TUE','WED','THU','FRI','SAT'][dt.getDay()];
  const mo = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][dt.getMonth()];
  $('date').textContent = `${wd} · ${mo} ${dt.getDate()} · ${dt.getFullYear()}`;

  if(state.isGuest){
    hello.textContent = '你好，同学 ☕';
    $('settings-uid').textContent = '游客';
    const n = state.accounts.length;
    $('settings-accounts-meta').textContent = n
      ? `游客模式 · ${n} 个学号 · 数据仅存本会话`
      : '游客模式 · 数据仅存本会话';
    $('settings-auth-btn').style.display = '';
    $('settings-auth-btn').textContent = '登录';
    $('settings-auth-btn').onclick = () => openSheet('login');
    $('logout-card').style.display = 'none';
  }else{
    hello.textContent = `你好，${state.uid} ☕`;
    $('settings-uid').textContent = state.uid;
    $('settings-accounts-meta').textContent = `已登录 · ${state.accounts.length} 个学号`;
    $('settings-auth-btn').style.display = 'none';
    $('logout-card').style.display = '';
  }
}

async function doAuth(){
  const uid = $('auth-uid').value.trim();
  const pass = $('auth-pass').value;
  if(!uid || !pass){ toast('请填写用户名和密码','error'); return; }
  const btn = $('auth-btn');
  btn.disabled = true;
  const labelOld = btn.textContent;
  btn.textContent = '...';
  const { ok, data } = await api(`/api/auth/${state.authMode}`, { method:'POST', body:{ username: uid, password: pass } });
  btn.disabled = false;
  btn.textContent = labelOld;
  if(ok){
    toast(data.message || '成功', 'success');
    state.isGuest = false;
    state.uid = data.uid;
    updateAuthUI();
    closeSheet();
    await loadAccounts();
    await loadHome();
  }else{
    toast(data.error || '失败', 'error');
  }
}

function switchAuth(mode){
  state.authMode = mode;
  document.querySelectorAll('.login-toggle button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  $('auth-btn').textContent = mode === 'login' ? '登录' : '注册';
}

async function doLogout(){
  await api('/api/auth/logout', { method:'POST' });
  state.isGuest = true;
  state.uid = '';
  state.accounts = [];
  state.currentPid = '';
  state.currentCfg = null;
  state.todayResv = null;
  updateAuthUI();
  renderAllAccountViews();
  renderHome();
  toast('已退出','info');
}

// ---------- accounts ----------
async function loadAccounts(){
  try{
    const { ok, data } = await api('/api/my/accounts');
    if(ok){
      state.accounts = Array.isArray(data) ? data : [];
      if(!state.currentPid && state.accounts.length){
        state.currentPid = state.accounts[0].pid;
      }
      if(state.currentPid && !state.accounts.find(a => a.pid === state.currentPid)){
        state.currentPid = state.accounts.length ? state.accounts[0].pid : '';
      }
    }else{
      state.accounts = [];
    }
  }catch(e){
    state.accounts = [];
  }
  renderAllAccountViews();
  if(state.currentPid){
    await loadAccountDetail(state.currentPid);
  }else{
    state.currentCfg = null;
    renderConfig();
    renderHome();
  }
}

function renderAllAccountViews(){
  const chip = $('pid-text');
  if(state.currentPid){
    chip.textContent = state.currentPid;
  }else{
    chip.textContent = '未添加';
  }
  const dot = $('pid-dot');
  if(dot){
    const a = state.accounts.find(x => x.pid === state.currentPid);
    const on = a && a.is_reserved === 'True';
    dot.classList.toggle('off', !on);
  }
  updateAuthUI();
}

async function loadAccountDetail(pid){
  try{
    const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(pid)}`);
    if(ok && data && data.pid){
      state.currentCfg = data;
    }else{
      state.currentCfg = null;
    }
  }catch(e){
    state.currentCfg = null;
  }
  renderConfig();
  renderHome();
  loadReservations();
}

function pickAcct(pid){
  state.currentPid = pid;
  toast('已切换到 ' + pid, 'success');
  closeSheet();
  renderAllAccountViews();
  loadAccountDetail(pid);
}

async function deleteAcct(pid){
  if(!confirm(`确定删除学号 ${pid}？`)) return;
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(pid)}`, { method:'DELETE' });
  if(ok){
    toast('已删除','success');
    if(state.currentPid === pid) state.currentPid = '';
    await loadAccounts();
    openSheet('accounts');
  }else{
    toast(data.error || '删除失败','error');
  }
}

async function verifyAdd(){
  const pidEl = $('new-pid'), vpnEl = $('new-vpn'), libEl = $('new-lib');
  const pid = pidEl.value.trim(), vpn = vpnEl.value, lib = libEl.value;
  if(!pid){ toast('请输入学号','error'); return; }
  if(!vpn || !lib){ toast('请填写两个密码','error'); return; }

  const btn = $('btn-verify-add');
  btn.disabled = true;
  btn.textContent = '验证中...';

  // Step 1: verify credentials before saving
  const vres = await api(`/api/my/accounts/${encodeURIComponent(pid)}/verify`, {
    method:'POST',
    body:{ vpn_password: vpn, lib_password: lib }
  });
  if(!vres.ok || vres.data.verified !== true){
    btn.disabled = false;
    btn.textContent = '验证并保存';
    toast(vres.data.error || '验证失败','error');
    return;
  }

  // Step 2: save (backend defaults: is_reserved=True, late_protection=False)
  const sres = await api(`/api/my/accounts/${encodeURIComponent(pid)}`, {
    method:'POST',
    body:{ vpn_password: vpn, lib_password: lib, mode: 'week_time', verified: true }
  });
  btn.disabled = false;
  btn.textContent = '验证并保存';
  if(sres.ok){
    toast('已添加学号 ' + pid,'success');
    state.currentPid = pid;
    closeSheet();
    await loadAccounts();
    if(state.isGuest && !localStorage.getItem('autolib_guest_data_notice_ack')){
      openSheet('guest-data-notice');
    }
  }else{
    toast(sres.data.error || '保存失败','error');
  }
}

// ---------- config ----------
function renderConfig(){
  const cfg = state.currentCfg;
  if(!cfg){
    $('cfg-empty').style.display = '';
    $('cfg-form').style.display = 'none';
    return;
  }
  $('cfg-empty').style.display = 'none';
  $('cfg-form').style.display = '';

  const mode = cfg.mode || 'week_time';
  state.cfgMode = (mode === 'week_time') ? 'week' : 'simple';
  document.querySelectorAll('#page-config .seg > button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === state.cfgMode);
  });
  $('week-mode').style.display = state.cfgMode === 'week' ? '' : 'none';
  $('simple-mode').style.display = state.cfgMode === 'simple' ? '' : 'none';

  // Week grid
  buildWeekGrid(cfg.time && cfg.time.week_time);

  // Simple mode (支持多段)
  const simpleList = $('simple-seg-list');
  if(simpleList){
    let simpleSegs = toSegments(cfg.time && cfg.time.tomorrow);
    if(!simpleSegs.length) simpleSegs = ['08:00-22:00'];
    simpleList.innerHTML = simpleSegs.map(simpleSegRowHtml).join('');
  }

  // Seats
  renderCfgSeats();

  // Toggles
  $('cfg-toggle-reserve').classList.toggle('on', cfg.is_reserved === 'True');
  $('cfg-toggle-lp').classList.toggle('on', cfg.late_protection === 'True');

  // Passwords
  $('cfg-vpn').value = cfg.vpn_password || '';
  $('cfg-lib').value = cfg.lib_password || '';

  $('cfg-pid-label').textContent = cfg.pid;
  $('cfg-verify-badge').textContent = cfg.verified ? '✓ 已验证' : '⚠ 未验证';
  $('cfg-verify-badge').className = 'pill ' + (cfg.verified ? 'ok' : 'warn');

  $('email-label').textContent = cfg.notify_email || '未设置';
}

function segmentRowHtml(seg, isoDay, disabled){
  const [rs, re] = seg.split('-');
  const [s, e] = clampRange(rs, re, isoDay);
  const { min, max } = timeBounds(isoDay);
  return `
    <div class="seg-row" data-seg>
      <input class="time-input" type="time" value="${s}" data-field="start" min="${min}" max="${max}" ${disabled?'disabled':''} style="padding:4px 8px;font-size:13px">
      <span class="to">→</span>
      <input class="time-input" type="time" value="${e}" data-field="end" min="${min}" max="${max}" ${disabled?'disabled':''} style="padding:4px 8px;font-size:13px">
      <button type="button" class="btn sm ghost seg-del" onclick="removeSeg(this)" ${disabled?'disabled':''}>×</button>
    </div>`;
}

function buildWeekGrid(weekTime){
  const grid = $('week-grid');
  grid.innerHTML = '';
  for(let i = 1; i <= 7; i++){
    const key = String(i);
    const rawVal = weekTime && weekTime[key];
    const isOff = rawVal === '休息' || rawVal === 'off';
    let segs = toSegments(rawVal);
    if(!segs.length) segs = [WEEK_DEFAULTS[i-1]];
    const row = document.createElement('div');
    row.className = 'box tight';
    row.dataset.day = i;
    row.innerHTML = `
      <div class="row-between">
        <div class="row-flex" style="gap:8px">
          <div class="toggle ${isOff?'':'on'}" data-field="day-on" onclick="tToggle(this, event)"></div>
          <div class="sub" style="font-weight:700">周${WEEK_LABELS[i-1]}${i===5?' <span class="tiny" style="color:var(--ink3)">· 最晚 20:00</span>':''}</div>
        </div>
        <button type="button" class="btn sm ghost" data-field="seg-add" onclick="addSeg(this)" ${isOff?'disabled':''}>+ 加时段</button>
      </div>
      <div class="seg-list mt" data-field="seg-list">
        ${segs.map(s => segmentRowHtml(s, i, isOff)).join('')}
      </div>`;
    grid.appendChild(row);
  }
  // 日 toggle 启用/停用时间输入与 + 按钮
  grid.querySelectorAll('[data-field="day-on"]').forEach(tg => {
    tg.addEventListener('click', () => {
      const row = tg.closest('[data-day]');
      const isOn = tg.classList.contains('on');
      row.querySelectorAll('input[type="time"]').forEach(inp => inp.disabled = !isOn);
      row.querySelectorAll('[data-field="seg-add"], .seg-del').forEach(b => b.disabled = !isOn);
    });
  });
}

function addSeg(btn){
  const row = btn.closest('[data-day]');
  if(!row) return;
  const isoDay = parseInt(row.dataset.day, 10);
  const list = row.querySelector('[data-field="seg-list"]');
  // 新段默认用该天的默认时段
  list.insertAdjacentHTML('beforeend', segmentRowHtml(WEEK_DEFAULTS[isoDay-1], isoDay, false));
}

function removeSeg(btn){
  const row = btn.closest('[data-day]');
  if(!row) return;
  const list = row.querySelector('[data-field="seg-list"]');
  const segs = list.querySelectorAll('[data-seg]');
  if(segs.length <= 1){
    toast('至少保留一段，关闭当天请用左侧开关','info');
    return;
  }
  btn.closest('[data-seg]').remove();
}

function addSimpleSeg(){
  const list = $('simple-seg-list');
  if(!list) return;
  list.insertAdjacentHTML('beforeend', simpleSegRowHtml('08:00-22:00'));
}

function removeSimpleSeg(btn){
  const list = $('simple-seg-list');
  if(!list) return;
  const segs = list.querySelectorAll('[data-seg]');
  if(segs.length <= 1){
    toast('至少保留一段','info');
    return;
  }
  btn.closest('[data-seg]').remove();
}

function simpleSegRowHtml(seg){
  const [rs, re] = seg.split('-');
  const [s, e] = clampRange(rs, re, null);
  return `
    <div class="seg-row" data-seg>
      <input class="time-input" type="time" value="${s}" data-field="start" min="${TIME_MIN}" max="${TIME_MAX_DEFAULT}">
      <span class="to">→</span>
      <input class="time-input" type="time" value="${e}" data-field="end" min="${TIME_MIN}" max="${TIME_MAX_DEFAULT}">
      <button type="button" class="btn sm ghost seg-del" onclick="removeSimpleSeg(this)">×</button>
    </div>`;
}

function renderCfgSeats(){
  const list = $('cfg-seats');
  list.innerHTML = '';
  const seats = (state.currentCfg && state.currentCfg.seat_list) || [];
  seats.forEach((s, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `<span class="ord">${i+1}</span>${escHtml(s)}<span class="x" onclick="removeCfgSeat(${i})">×</span>`;
    list.appendChild(chip);
  });
  const add = document.createElement('span');
  add.className = 'chip add';
  add.textContent = '+ 加座位';
  add.onclick = () => openSheet('seat-picker');
  list.appendChild(add);
}

function removeCfgSeat(i){
  if(!state.currentCfg) return;
  state.currentCfg.seat_list = (state.currentCfg.seat_list || []).filter((_, idx) => idx !== i);
  renderCfgSeats();
}

function setCfgMode(m){
  state.cfgMode = m;
  document.querySelectorAll('#page-config .seg > button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === m);
  });
  $('week-mode').style.display = m === 'week' ? '' : 'none';
  $('simple-mode').style.display = m === 'simple' ? '' : 'none';
}

async function saveCfg(){
  if(!state.currentPid || !state.currentCfg){
    toast('请先添加学号','error');
    openSheet('accounts');
    return;
  }

  // 收集 week grid（每天为段数组，周五结束时间最晚 20:00）
  const wt = {};
  document.querySelectorAll('#week-grid [data-day]').forEach(row => {
    const day = parseInt(row.dataset.day, 10);
    const on = row.querySelector('[data-field="day-on"]').classList.contains('on');
    if(!on){
      wt[day] = '休息';
      return;
    }
    const segs = [];
    row.querySelectorAll('[data-seg]').forEach(seg => {
      const s = seg.querySelector('[data-field="start"]').value;
      const e = seg.querySelector('[data-field="end"]').value;
      const [cs, ce] = clampRange(s, e, day);
      segs.push(`${cs}-${ce}`);
    });
    wt[day] = segs.length ? segs : [WEEK_DEFAULTS[day-1]];
  });

  const mode = state.cfgMode === 'week' ? 'week_time' : 'tomorrow';
  let simpleSegs = [];
  if(mode === 'tomorrow'){
    document.querySelectorAll('#simple-seg-list [data-seg]').forEach(seg => {
      const s = seg.querySelector('[data-field="start"]').value;
      const e = seg.querySelector('[data-field="end"]').value;
      const [cs, ce] = clampRange(s, e, null);
      simpleSegs.push(`${cs}-${ce}`);
    });
    if(!simpleSegs.length) simpleSegs = ['08:00-22:00'];
  }
  const timeCfg = mode === 'week_time'
    ? { week_time: wt }
    : { tomorrow: simpleSegs, week_time: wt };

  const body = {
    vpn_password: $('cfg-vpn').value,
    lib_password: $('cfg-lib').value,
    seat_list: state.currentCfg.seat_list || [],
    mode,
    time: timeCfg,
    is_reserved: $('cfg-toggle-reserve').classList.contains('on') ? 'True' : 'False',
    late_protection: $('cfg-toggle-lp').classList.contains('on') ? 'True' : 'False',
  };

  const btn = $('btn-save-cfg');
  btn.disabled = true;
  btn.textContent = '保存中...';
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}`, { method:'POST', body });
  btn.disabled = false;
  btn.textContent = '保存配置';
  if(ok){
    toast('配置已保存','success');
    await loadAccounts();
    setTimeout(() => go('home'), 400);
  }else{
    toast(data.error || '保存失败','error');
  }
}

async function reserveNow(){
  if(!state.currentPid){ toast('请先添加学号','error'); return; }
  if(!confirm('立即用当前配置抢一次座？')) return;

  toast('正在用当前配置抢座...','info');
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/reserve_now`, { method:'POST' });
  if(ok){
    toast(data.success ? '预约成功 🎉' : '预约已完成', data.success ? 'success' : 'info');
    loadReservations();
    loadNotices();
    if(data.result) alert('预约结果：\n\n' + data.result);
  }else{
    toast(data.error || '预约失败','error');
  }
}

// ---------- reservations (today + tomorrow) ----------
function localDateStr(offsetDays){
  const d = new Date(Date.now() + (offsetDays || 0) * 86400000);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function loadReservations(){
  if(!state.currentPid || !state.currentCfg || !state.currentCfg.verified){
    state.todayResv = null;
    state.tomorrowResv = null;
    renderHome();
    return;
  }
  renderTodayCard({ loading: true });
  renderTomorrowCard({ loading: true });
  try{
    const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/reservations`);
    if(ok && Array.isArray(data.reservations)){
      const todayStr = localDateStr(0);
      const tmrStr = localDateStr(1);
      state.todayResv = data.reservations.find(r => (r.resvBeginTime || '').startsWith(todayStr)) || null;
      state.tomorrowResv = data.reservations.find(r => (r.resvBeginTime || '').startsWith(tmrStr)) || null;
    }else{
      state.todayResv = null;
      state.tomorrowResv = null;
    }
  }catch(e){
    state.todayResv = null;
    state.tomorrowResv = null;
  }
  renderHome();
}


async function toggleArrived(){
  if(!state.currentPid){ toast('请先添加学号','error'); return; }
  const btn = $('btn-arrived');
  btn.disabled = true;
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/arrived`, { method:'POST' });
  btn.disabled = false;
  if(ok){
    if(data.arrived){
      toast('已标记到馆，迟到保护今日不触发','success');
    }else{
      toast('已取消到馆标记','info');
    }
    await loadAccountDetail(state.currentPid);
  }else{
    toast('操作失败','error');
  }
}

async function doCancel(){
  closeSheet();
  if(!state.todayResv){ toast('没有可取消的预约','info'); return; }
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/cancel`, {
    method:'POST', body:{ uuid: state.todayResv.uuid }
  });
  toast(data.message || (ok ? '已取消' : '失败'), (ok && data.success) ? 'success' : 'error');
  if(ok && data.success){
    state.todayResv = null;
    renderHome();
  }
}

// ---------- home rendering ----------
function renderHome(){
  const d = new Date();
  $('today-date-label').textContent = `周${WEEK_LABELS[d.getDay()===0?6:d.getDay()-1]} · ${d.getMonth()+1}月${d.getDate()}日`;
  const tmr = new Date(d.getTime() + 86400000);
  const tmrLabelEl = $('tmr-date-label');
  if(tmrLabelEl){
    tmrLabelEl.textContent = `周${WEEK_LABELS[tmr.getDay()===0?6:tmr.getDay()-1]} · ${tmr.getMonth()+1}月${tmr.getDate()}日`;
  }
  $('tmr-n').textContent = tmr.getDate();
  $('tmr-wd').textContent = ['SUN','MON','TUE','WED','THU','FRI','SAT'][tmr.getDay()];

  renderTodayCard({});
  renderTomorrowCard({});
  renderTomorrowStrip();
  renderWeekPreview();
}

function renderTodayCard(opt){
  const card = $('today-card');
  const empty = $('today-empty');
  const cfg = state.currentCfg;

  if(opt.loading){
    card.style.display = '';
    empty.style.display = 'none';
    card.innerHTML = `<div class="label">今日座位</div><div class="sub" style="margin-top:8px">加载中...</div>`;
    return;
  }

  if(!cfg){
    card.style.display = 'none';
    empty.style.display = '';
    empty.innerHTML = `
      <div class="h2" style="color:var(--ink3)">还没有学号</div>
      <div class="sub" style="margin-top:6px">添加学号后系统会自动抢座</div>
      <div class="actions" style="justify-content:center">
        <button class="btn accent" onclick="openSheet('add-pid')">+ 添加学号</button>
      </div>`;
    return;
  }

  const resv = state.todayResv;
  if(!resv){
    card.style.display = 'none';
    empty.style.display = '';
    const reserveOn = cfg.is_reserved === 'True';
    empty.innerHTML = `
      <div class="h2" style="color:var(--ink3)">今日暂无预约</div>
      <div class="sub" style="margin-top:6px">${reserveOn ? '按当前配置今日无座位，或抢座进行中' : '自动预约已暂停'}</div>
      <div class="actions" style="justify-content:center">
        <button class="btn accent" onclick="reserveNow()">⚡ 立即预约</button>
      </div>`;
    return;
  }

  const seat = (resv.devInfo && resv.devInfo.devName) || '未知';
  const m = seat.match(/^(\d+F)[-·]?(.*)$/);
  const zone = m ? m[1] : '';
  const num = m ? m[2] : seat;
  const bt = (resv.resvBeginTime || '').split(' ')[1] || '';
  const et = (resv.resvEndTime || '').split(' ')[1] || '';
  const bhm = bt.slice(0,5), ehm = et.slice(0,5);
  const hours = (bhm && ehm) ? (parseInt(ehm,10) - parseInt(bhm,10)) : '';
  const status = fmtResvStatus(resv.resvStatus);
  const canCancel = resv.resvStatus <= 3 || resv.resvStatus === 1027 || resv.resvStatus === 3141;
  const lpOn = cfg.late_protection === 'True';
  const arrived = cfg.arrived_date === new Date().toISOString().slice(0,10);

  card.style.display = '';
  empty.style.display = 'none';
  card.innerHTML = `
    <div class="stamp">TODAY</div>
    <div class="label">今日座位</div>
    <div class="seat">${zone ? `<span class="zone">${escHtml(zone)}</span>` : ''}${escHtml(num)}</div>
    <div class="time">${bhm} — ${ehm}${hours ? ` · ${hours}小时` : ''}</div>
    <div class="meta-row">
      <span class="pill ok"><span class="dot"></span>${escHtml(status)}</span>
      ${lpOn ? '<span class="pill accent">🛡 迟到保护</span>' : ''}
      ${arrived ? '<span class="pill ok">✓ 已到馆</span>' : ''}
    </div>
    <div class="actions">
      ${lpOn ? `<button class="btn ${arrived?'primary':'accent'} lg grow" id="btn-arrived" onclick="toggleArrived()">${arrived ? '✓ 已到馆' : '✓ 我已到馆'}</button>` : ''}
      ${canCancel ? `<button class="btn sm" onclick="openSheet('cancel')">取消</button>` : ''}
    </div>`;
}

function renderTomorrowCard(opt){
  const card = $('tmr-card');
  const empty = $('tmr-empty');
  if(!card || !empty) return;
  const cfg = state.currentCfg;

  if(opt.loading){
    card.style.display = '';
    empty.style.display = 'none';
    card.innerHTML = `<div class="label">明日座位</div><div class="sub" style="margin-top:8px">加载中...</div>`;
    return;
  }

  if(!cfg){
    card.style.display = 'none';
    empty.style.display = 'none';
    return;
  }

  const resv = state.tomorrowResv;
  if(!resv){
    card.style.display = 'none';
    empty.style.display = '';
    const reserveOn = cfg.is_reserved === 'True';
    const msg = reserveOn
      ? '将按配置自动预约，结果会在抢座后可见'
      : '自动预约已暂停，去配置页开启';
    empty.innerHTML = `
      <div class="h2" style="color:var(--ink3)">明日暂无预约</div>
      <div class="sub" style="margin-top:6px">${msg}</div>`;
    return;
  }

  const seat = (resv.devInfo && resv.devInfo.devName) || '未知';
  const m = seat.match(/^(\d+F)[-·]?(.*)$/);
  const zone = m ? m[1] : '';
  const num = m ? m[2] : seat;
  const bt = (resv.resvBeginTime || '').split(' ')[1] || '';
  const et = (resv.resvEndTime || '').split(' ')[1] || '';
  const bhm = bt.slice(0,5), ehm = et.slice(0,5);
  const hours = (bhm && ehm) ? (parseInt(ehm,10) - parseInt(bhm,10)) : '';
  const status = fmtResvStatus(resv.resvStatus);

  card.style.display = '';
  empty.style.display = 'none';
  card.innerHTML = `
    <div class="stamp">TMR</div>
    <div class="label">明日座位</div>
    <div class="seat">${zone ? `<span class="zone">${escHtml(zone)}</span>` : ''}${escHtml(num)}</div>
    <div class="time">${bhm} — ${ehm}${hours ? ` · ${hours}小时` : ''}</div>
    <div class="meta-row">
      <span class="pill ok"><span class="dot"></span>${escHtml(status)}</span>
    </div>`;
}

function fmtResvStatus(s){
  return { 1:'待签到', 2:'使用中', 3:'暂离', 4:'已结束', 5:'已取消', 1027:'未开始', 1093:'使用中', 3141:'暂离' }[s] || `状态(${s})`;
}

function renderTomorrowStrip(){
  const cfg = state.currentCfg;
  const tt = $('tmr-title');
  const ss = $('tmr-sub');
  if(!cfg){ tt.textContent = '添加学号后查看'; ss.textContent = state.isGuest ? '游客数据仅存本会话' : ''; return; }
  if(cfg.is_reserved !== 'True'){ tt.textContent = '自动预约已暂停'; ss.textContent = '去配置页开启「自动预约」'; return; }

  const seats = cfg.seat_list || [];
  const seat = seats[0] || '—';
  let segs = [];
  const tmr = new Date(Date.now() + 86400000);
  const isoNum = tmr.getDay() === 0 ? 7 : tmr.getDay();
  if(cfg.mode === 'tomorrow' && cfg.time){
    segs = toSegments(cfg.time.tomorrow);
  }else if(cfg.time && cfg.time.week_time){
    segs = toSegments(cfg.time.week_time[String(isoNum)]);
  }
  if(isoNum === 5) segs = segs.map(friCap);
  const timeLabel = segs.length
    ? (segs.length > 1 ? `${segs.length}段: ${segs.join(' · ')}` : segs[0])
    : '—';
  tt.textContent = '按配置自动预约';
  ss.textContent = `明早抢 ${seat} · ${timeLabel}`;
}

function renderWeekPreview(){
  const host = $('week-rows');
  host.innerHTML = '';
  const cfg = state.currentCfg;
  const today = new Date().getDay() === 0 ? 7 : new Date().getDay();
  const tmr = today === 7 ? 1 : today + 1;
  const mode = (cfg && cfg.mode) || 'week_time';
  const reserveOn = cfg && cfg.is_reserved === 'True';

  // Update mode hint
  const hint = $('week-mode-hint');
  if(hint){
    if(!cfg) hint.textContent = '06 — 22 时';
    else if(!reserveOn) hint.textContent = '自动预约已暂停';
    else hint.textContent = mode === 'tomorrow' ? '统一时段模式' : '按星期模式';
  }
  const footer = $('week-footer-hint');
  if(footer){
    if(!cfg) footer.textContent = '💡 添加学号后查看配置预览';
    else if(!reserveOn) footer.textContent = '💡 开启自动预约后这里会亮起';
    else if(mode === 'tomorrow') footer.textContent = '💡 每天都按同一时段预约';
    else footer.textContent = '💡 每天按配置的时段预约';
  }

  for(let i = 1; i <= 7; i++){
    // 计算当天段列表
    let segs = [];
    if(cfg && reserveOn){
      if(mode === 'tomorrow'){
        segs = toSegments(cfg.time && cfg.time.tomorrow);
        if(!segs.length) segs = ['08:00-22:00'];
      }else{
        segs = toSegments(cfg.time && cfg.time.week_time && cfg.time.week_time[String(i)]);
      }
    }
    if(i === 5) segs = segs.map(friCap);
    const active = segs.length > 0;

    const isToday = i === today;
    const isTmr = i === tmr;
    const tag = isToday
      ? '<span class="tag">今</span>'
      : (isTmr ? '<span class="tag dim">明</span>' : '');
    const dayClass = isToday ? 'today' : (active ? '' : 'off');

    if(!active){
      host.innerHTML += `
        <div class="row">
          <span class="day ${dayClass}">${tag}周${WEEK_LABELS[i-1]}</span>
          <div class="track">
            <div class="bar empty" style="left:0%;width:100%"></div>
          </div>
        </div>`;
      continue;
    }

    let barClass;
    if(mode === 'tomorrow') barClass = 'today';
    else if(isToday) barClass = 'today';
    else if(isTmr) barClass = 'tomorrow-b';
    else barClass = 'active';
    const showLabel = (isToday || isTmr || mode === 'tomorrow');

    const bars = segs.map(seg => {
      const [s, e] = seg.split('-');
      const sh = parseInt(s.split(':')[0], 10) + parseInt(s.split(':')[1], 10)/60;
      const eh = parseInt(e.split(':')[0], 10) + parseInt(e.split(':')[1], 10)/60;
      const left = Math.max(0, (sh - 8) / 14 * 100);
      const width = Math.max(2, (eh - sh) / 14 * 100);
      // 多段时标签可能挤不下，仅在单段时显示
      const lbl = (showLabel && segs.length === 1)
        ? `<span class="lbl">${s.slice(0,5)}-${e.slice(0,5)}</span>`
        : '';
      return `<div class="bar ${barClass}" style="left:${left}%;width:${width}%">${lbl}</div>`;
    }).join('');
    const segCountTip = segs.length > 1
      ? `<span class="tiny" style="margin-left:4px;color:var(--ink3)">×${segs.length}</span>`
      : '';
    host.innerHTML += `
      <div class="row">
        <span class="day ${dayClass}">${tag}周${WEEK_LABELS[i-1]}${segCountTip}</span>
        <div class="track">${bars}</div>
      </div>`;
  }
}

// ---------- tomorrow expander ----------
function toggleTmr(){
  state.tmrOpen = !state.tmrOpen;
  $('tmr-strip').classList.toggle('open', state.tmrOpen);
  $('tmr-body').classList.toggle('open', state.tmrOpen);
  if(state.tmrOpen) renderTomorrowBody();
}

function renderTomorrowBody(){
  const cfg = state.currentCfg;
  const tmr = new Date(Date.now() + 86400000);
  const isoNum = tmr.getDay() === 0 ? 7 : tmr.getDay();
  const iso = String(isoNum);
  let segs = [];
  if(cfg && cfg.time){
    if(cfg.mode === 'tomorrow') segs = toSegments(cfg.time.tomorrow);
    else if(cfg.time.week_time) segs = toSegments(cfg.time.week_time[iso]);
  }
  if(!segs.length) segs = ['08:00-22:00'];
  const [rs, re] = segs[0].split('-');
  const [s, e] = clampRange(rs, re, isoNum);
  const { min, max } = timeBounds(isoNum);
  const startEl = $('tmr-start');
  const endEl = $('tmr-end');
  startEl.min = min; startEl.max = max; startEl.value = s;
  endEl.min = min; endEl.max = max; endEl.value = e;

  // 多段提示
  const hint = $('tmr-multi-hint');
  if(hint){
    if(segs.length > 1){
      hint.textContent = `⚠ 当前有 ${segs.length} 段，快速编辑仅保留首段。多段请在配置中调整`;
      hint.style.display = '';
    }else{
      hint.style.display = 'none';
    }
  }

  const list = $('tmr-seats');
  list.innerHTML = '';
  const seats = (cfg && cfg.seat_list) || [];
  seats.forEach((name, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `<span class="ord">${i+1}</span>${escHtml(name)}`;
    list.appendChild(chip);
  });
  if(!seats.length){
    const tip = document.createElement('span');
    tip.className = 'tiny';
    tip.style.color = 'var(--ink3)';
    tip.textContent = '还没有配置座位';
    list.appendChild(tip);
  }
}

async function saveTmr(){
  if(!state.currentPid || !state.currentCfg){
    toast('请先添加学号','error'); return;
  }
  const rawS = $('tmr-start').value, rawE = $('tmr-end').value;
  if(!rawS || !rawE){ toast('时间无效','error'); return; }
  const tmr = new Date(Date.now() + 86400000);
  const isoNum = tmr.getDay() === 0 ? 7 : tmr.getDay();
  const [s, e] = clampRange(rawS, rawE, isoNum);
  if(s >= e){ toast('时间无效','error'); return; }
  const body = {
    mode: 'tomorrow',
    time: Object.assign({}, state.currentCfg.time || {}, { tomorrow: [`${s}-${e}`] }),
    seat_list: state.currentCfg.seat_list || [],
  };
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}`, { method:'POST', body });
  if(ok){
    toast('明日已更新（已切换到统一时段模式）','success');
    closeTmr();
    await loadAccountDetail(state.currentPid);
    await loadAccounts();
  }else{
    toast(data.error || '保存失败','error');
  }
}

function closeTmr(){
  state.tmrOpen = false;
  $('tmr-strip').classList.remove('open');
  $('tmr-body').classList.remove('open');
}

// ---------- notices ----------
function toggleNotices(){
  state.noticesCollapsed = !state.noticesCollapsed;
  $('notice-list').style.display = state.noticesCollapsed ? 'none' : '';
  $('notice-toggle').textContent = state.noticesCollapsed ? '展开' : '折叠';
}

async function loadNotices(){
  try{
    const promises = [fetch('/api/announcements').catch(() => null)];
    promises.push(fetch('/api/my/reservation_results').catch(() => null));
    const res = await Promise.all(promises);
    const anns = (res[0] && res[0].ok) ? await res[0].json() : [];
    const results = (res[1] && res[1].ok) ? await res[1].json() : [];
    renderNotices(anns, results);
  }catch(e){
    $('notice-list').innerHTML = '<div class="empty">通知加载失败</div>';
  }
}

function renderNotices(anns, results){
  const list = $('notice-list');
  const items = [];
  const colorMap = { info:'accent', success:'ok', warning:'warn', danger:'var(--danger)' };

  (anns || []).forEach(a => {
    const lv = a.level || 'info';
    const border = { info:'var(--accent)', success:'var(--ok)', warning:'var(--warn)', danger:'var(--danger)' }[lv] || 'var(--accent)';
    const pill = { info:'accent', success:'ok', warning:'warn', danger:'accent' }[lv] || 'accent';
    const pin = a.pinned ? '<span class="pill" style="border-color:#a78bfa;color:#a78bfa">置顶</span>' : '';
    items.push(`
      <div class="box tight" style="border-left:5px solid ${border}">
        <div class="row-flex" style="gap:6px;flex-wrap:wrap">
          <span class="pill ${pill}">公告</span>${pin}
          <div class="sub" style="font-weight:700">${escHtml(a.title)}</div>
        </div>
        <div class="t" style="margin-top:4px;white-space:pre-wrap">${escHtml(a.content)}</div>
        <div class="tiny" style="margin-top:6px">${escHtml(a.updated_at || a.created_at || '')}</div>
      </div>`);
  });

  (results || []).forEach(r => {
    if(!r.result) return;
    const ok = r.success;
    const border = ok ? 'var(--ok)' : 'var(--danger)';
    items.push(`
      <div class="box tight" style="border-left:5px solid ${border}">
        <div class="row-flex" style="gap:6px">
          <span class="pill ${ok?'ok':'accent'}">${ok ? '预约成功' : '预约失败'}</span>
          <div class="sub" style="font-weight:700">学号 ${escHtml(r.pid)}</div>
        </div>
        <div class="t" style="margin-top:4px;white-space:pre-wrap">${escHtml(r.result)}</div>
        <div class="tiny" style="margin-top:6px">${escHtml(r.updated_at || '')}</div>
      </div>`);
  });

  list.innerHTML = items.length ? items.join('') : '<div class="empty">暂无通知</div>';
}

// ---------- seats ----------
async function loadSeats(){
  try{
    const path = state.isGuest ? '/api/public/seats' : '/api/seats';
    const { ok, data } = await api(path);
    state.allSeats = (ok && data.seats) ? data.seats : {};
  }catch(e){
    state.allSeats = {};
  }
}

function sortedZones(){
  return Object.keys(state.allSeats).sort((a, b) => {
    const fa = a.match(/\d+/) ? parseInt(a.match(/\d+/)[0], 10) : 99;
    const fb = b.match(/\d+/) ? parseInt(b.match(/\d+/)[0], 10) : 99;
    return fa - fb || a.localeCompare(b);
  });
}

function onZone(v){
  const sel = $('sp-seat');
  const seats = state.allSeats[v] || [];
  sel.innerHTML = seats.length ? seats.map(s => `<option value="${escHtml(s)}">${escHtml(s)}</option>`).join('') : '<option>无座位</option>';
}

function addSeat(){
  const z = $('sp-zone').value;
  const s = $('sp-seat').value;
  if(!z || !s){ toast('请先选择','error'); return; }
  if(!state.currentCfg){ toast('请先选择学号','error'); return; }
  const seats = state.currentCfg.seat_list || [];
  if(seats.includes(s)){ toast('座位已存在','error'); closeSheet(); return; }
  seats.push(s);
  state.currentCfg.seat_list = seats;
  renderCfgSeats();
  toast('已添加 ' + s, 'success');
  closeSheet();
}

// ---------- notify ----------
async function saveEmail(){
  if(!state.currentPid || !state.currentCfg){ toast('请先添加学号','error'); return; }
  const v = $('em-input').value.trim();
  const body = { notify_email: v };
  const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}`, { method:'POST', body });
  if(ok){
    toast('邮箱已保存','success');
    state.currentCfg.notify_email = v;
    $('email-label').textContent = v || '未设置';
    closeSheet();
  }else{
    toast(data.error || '保存失败','error');
  }
}

// ---------- toggles ----------
function tToggle(el, e){
  if(e) e.stopPropagation();
  el.classList.toggle('on');
}

function toggleLP(el, e){
  if(e) e.stopPropagation();
  const isOn = el.classList.contains('on');
  // Turning OFF is always direct
  if(isOn){
    el.classList.remove('on');
    return;
  }
  // Turning ON: show warning on first use
  const ACK_KEY = 'autolib_lp_warning_ack';
  if(localStorage.getItem(ACK_KEY) === '1'){
    el.classList.add('on');
    return;
  }
  state.pendingLPToggle = el;
  openSheet('lp-warning');
}

function acknowledgeLP(){
  localStorage.setItem('autolib_lp_warning_ack', '1');
  const el = state.pendingLPToggle;
  if(el) el.classList.add('on');
  state.pendingLPToggle = null;
  closeSheet();
}

function cancelLP(){
  state.pendingLPToggle = null;
  closeSheet();
}

function ackGuestDataNotice(){
  localStorage.setItem('autolib_guest_data_notice_ack', '1');
  closeSheet();
}

// ---------- sheets ----------
const SHEETS = {
  'guest-data-notice': () => `
    <div class="grab"></div>
    <h3>📦 关于你的数据安全</h3>
    <div class="desc">你现在是<strong>游客模式</strong>，没有注册网站账号。</div>
    <div class="box tight" style="margin-top:12px">
      <div class="t">你的学号和密码<strong>不保存在手机或电脑上</strong>，而是加密存放在服务器里。</div>
      <div class="t" style="margin-top:8px">但这份数据是和你<strong>当前的浏览器绑定</strong>的——如果你清除了浏览器数据、换了设备，或者很久没打开，这些数据就找不回来了。</div>
      <div class="t" style="margin-top:8px">👉 建议去「设置」页面注册一个网站账号（不需要手机号），这样数据会永久保存，换手机也能用。</div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="ackGuestDataNotice()">我知道了</button>
      <button class="btn accent grow" onclick="ackGuestDataNotice(); go('settings'); openSheet('login')">去注册账号</button>
    </div>
  `,
  login: () => `
    <div class="grab"></div>
    <h3>登录 / 注册</h3>
    <div class="desc">用户名仅用于管理这个网页，不是你的学号。</div>
    <div class="seg login-toggle" style="margin-bottom:14px">
      <button class="on" data-mode="login" onclick="switchAuth('login')">登录</button>
      <button data-mode="register" onclick="switchAuth('register')">注册</button>
    </div>
    <div class="col gap-sm">
      <div class="field"><label>用户名</label><input type="text" id="auth-uid" placeholder="你的用户名"></div>
      <div class="field"><label>密码</label><input type="password" id="auth-pass" placeholder="至少4位"></div>
    </div>
    <button class="btn accent mt-lg" id="auth-btn" style="width:100%" onclick="doAuth()">登录</button>
  `,
  accounts: () => `
    <div class="grab"></div>
    <h3>切换学号</h3>
    <div class="desc">多学号用户可以在这里快速切换。</div>
    <div class="col gap-sm">
      ${state.accounts.map(a => {
        const active = a.pid === state.currentPid;
        const on = a.is_reserved === 'True';
        const v = a.verified === true;
        return `
        <div class="box tight clickable ${active?'accent':''}" onclick="pickAcct('${escHtml(a.pid)}')">
          <div class="row-between">
            <div>
              <div class="mono" style="font-size:15px;font-weight:600">${escHtml(a.pid)}</div>
              <div class="tiny" style="margin-top:2px">${a.mode==='week_time'?'按星期':'统一时段'} · ${on?'运行中':'已暂停'} · ${v?'已验证':'未验证'}</div>
            </div>
            <div class="row-flex" style="gap:6px">
              ${active?'<span class="pill accent">当前</span>':'<span class="sub" style="color:var(--ink3)">›</span>'}
              <button class="btn sm danger" onclick="event.stopPropagation();deleteAcct('${escHtml(a.pid)}')">删除</button>
            </div>
          </div>
        </div>`;
      }).join('')}
      <div class="box tight ghost clickable" style="text-align:center" onclick="openSheet('add-pid')">
        <div class="sub" style="color:var(--ink3)">+ 添加学号</div>
      </div>
    </div>
  `,
  'add-pid': () => `
    <div class="grab"></div>
    <h3>添加新学号</h3>
    <div class="desc">填写学号和两个密码，我们会先验证再保存。</div>
    <div class="col gap-sm">
      <div class="field"><label>学号</label><input type="text" id="new-pid" placeholder="18210xxxxx"></div>
      <div class="field"><label>VPN 密码（统一身份认证）</label><input type="password" id="new-vpn"></div>
      <div class="field"><label>图书馆密码（IC 空间系统）</label><input type="password" id="new-lib"></div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" id="btn-verify-add" onclick="verifyAdd()">验证并保存</button>
    </div>
  `,
  cancel: () => `
    <div class="grab"></div>
    <h3>取消今日预约？</h3>
    <div class="desc">取消后座位会释放。如果只是临时外出，可以不用取消。</div>
    <div class="row-flex mt">
      <button class="btn ghost grow" onclick="closeSheet()">再想想</button>
      <button class="btn danger grow" onclick="doCancel()">确认取消</button>
    </div>
  `,
  'seat-picker': () => {
    const zones = sortedZones();
    return `
    <div class="grab"></div>
    <h3>添加座位</h3>
    <div class="desc">先选楼层，再选座位号。</div>
    <div class="col gap-sm">
      <div class="field"><label>楼层 / 区域</label>
        <select id="sp-zone" onchange="onZone(this.value)">
          <option value="">选择...</option>
          ${zones.map(z => `<option value="${escHtml(z)}">${escHtml(z)} (${state.allSeats[z].length}个)</option>`).join('')}
        </select>
      </div>
      <div class="field"><label>座位号</label>
        <select id="sp-seat"><option>先选楼层</option></select>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="addSeat()">添加</button>
    </div>`;
  },
  email: () => `
    <div class="grab"></div>
    <h3>邮箱通知</h3>
    <div class="desc">预约结果通过邮件推送。留空即不发送。</div>
    <div class="field"><label>邮箱地址</label><input type="email" id="em-input" value="${escHtml((state.currentCfg && state.currentCfg.notify_email) || '')}"></div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="saveEmail()">保存</button>
    </div>
  `,
  'lp-info': () => `
    <div class="grab"></div>
    <h3>🛡 关于迟到保护</h3>
    <div class="desc">开启后，系统会在你预约开始前检查是否到馆。</div>
    <div class="col gap-sm">
      <div class="box tight" style="border-left:4px solid var(--accent)">
        <div class="sub" style="font-weight:700">✓ 最多保护 1 小时</div>
        <div class="t">未按时到馆则自动把预约推迟 1 小时为你保留座位</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--ok)">
        <div class="sub" style="font-weight:700">✓ 到馆后手动确认</div>
        <div class="t">请点击主页的「我已到馆」按钮避免误操作</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--danger)">
        <div class="sub" style="font-weight:700">⚠ 1 小时后仍未到</div>
        <div class="t">系统将自动释放预约，杜绝恶意占座</div>
      </div>
    </div>
    <button class="btn primary mt-lg" style="width:100%" onclick="closeSheet()">我知道了</button>
  `,
  'lp-warning': () => `
    <div class="grab"></div>
    <h3>🛡 关于迟到保护</h3>
    <div class="desc">开启后，系统会在你预约开始前检查是否到馆。</div>
    <div class="col gap-sm">
      <div class="box tight" style="border-left:4px solid var(--accent)">
        <div class="sub" style="font-weight:700">✓ 最多保护 1 小时</div>
        <div class="t">未按时到馆则自动把预约推迟 1 小时为你保留座位</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--ok)">
        <div class="sub" style="font-weight:700">✓ 到馆后手动确认</div>
        <div class="t">请点击主页的「我已到馆」按钮避免误操作</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--danger)">
        <div class="sub" style="font-weight:700">⚠ 1 小时后仍未到</div>
        <div class="t">系统将自动释放预约，杜绝恶意占座</div>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="cancelLP()">取消</button>
      <button class="btn accent grow" onclick="acknowledgeLP()">我已知晓，永久关闭</button>
    </div>
  `,
};

function openSheet(name){
  const sc = $('scrim');
  const content = $('sheet-content');
  const tpl = SHEETS[name];
  content.innerHTML = tpl ? tpl() : '<div class="grab"></div><h3>未实现</h3>';
  sc.classList.add('show');
  if(name === 'lp-info' || name === 'lp-warning' || name === 'cancel' || name === 'guest-data-notice') sc.classList.add('center');
  else sc.classList.remove('center');
}
function closeSheet(){ $('scrim').classList.remove('show'); }

// ---------- home loader ----------
async function loadHome(){
  renderHome();
  loadNotices();
}

// ---------- init ----------
async function init(){
  // Wire scrim click-outside
  $('scrim').addEventListener('click', e => { if(e.target.id === 'scrim') closeSheet(); });

  await checkAuth();
  await loadSeats();
  await loadAccounts();
  renderHome();
  loadNotices();
}

init();
