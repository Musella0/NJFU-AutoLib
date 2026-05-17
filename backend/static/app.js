// ============ AutoLib · 手绘风 UI · 前端逻辑 ============

const state = {
  page: 'home',
  tmrOpen: false,
  noticesCollapsed: false,
  isGuest: true,
  uid: '',
  nickname: '',       // 可选显示名，覆盖 uid 显示
  accounts: [],       // list of account summaries
  currentPid: '',     // currently selected pid
  currentCfg: null,   // full detail for currentPid
  allSeats: {},       // { zone: [seatName, ...] }
  todayResv: null,    // today's live reservation (if any)
  tomorrowResv: null, // tomorrow's live reservation (if any)
  cfgMode: 'week',    // UI: 'week' or 'simple'
  authMode: 'login',
  napConfig: { start_time: '14:00', end_time: '', seat: '', auto_daily: false, trigger_time: '12:00' },
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

// 30 分钟档位，返回 [min, max] 闭区间内所有 "HH:MM"
function buildTimeOptions(min, max){
  const toMin = t => {
    const [h, m] = t.split(':').map(Number);
    return h * 60 + m;
  };
  const fmt = n => `${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
  const lo = toMin(min), hi = toMin(max);
  const opts = [];
  for(let m = lo; m <= hi; m += 30) opts.push(fmt(m));
  return opts;
}

// 生成 <option> HTML；selected 不在列表里时向下对齐到 ≤ selected 的最大档位
function timeOptionsHtml(min, max, selected){
  const opts = buildTimeOptions(min, max);
  let pick = selected;
  if(!opts.includes(pick)){
    pick = opts[0];
    for(const o of opts){ if(o <= selected) pick = o; else break; }
  }
  return opts.map(o => `<option value="${o}"${o===pick?' selected':''}>${o}</option>`).join('');
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
      state.nickname = data.nickname || '';
    }else{
      state.isGuest = true;
      state.uid = data.uid || '';
      state.nickname = '';
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

  const profileBtn = $('settings-profile-btn');
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
    if(profileBtn) profileBtn.style.display = 'none';
    $('logout-card').style.display = 'none';
  }else{
    const name = state.nickname || state.uid;
    hello.textContent = `你好，${name} ☕`;
    $('settings-uid').textContent = name;
    const subMeta = state.nickname
      ? `已登录 · @${state.uid} · ${state.accounts.length} 个学号`
      : `已登录 · ${state.accounts.length} 个学号`;
    $('settings-accounts-meta').textContent = subMeta;
    $('settings-auth-btn').style.display = 'none';
    if(profileBtn) profileBtn.style.display = '';
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
    state.nickname = data.nickname || '';
    updateAuthUI();
    closeSheet();
    await loadAccounts();
    await loadHome();
  }else{
    toast(data.error || '失败', 'error');
  }
}

async function saveProfile(){
  const nick = $('pf-nick').value.trim();
  const pw = $('pf-pass').value;
  const nickChanged = nick !== (state.nickname || '');
  if(!pw && !nickChanged){
    toast('没有要更新的内容','info');
    return;
  }
  const body = {};
  if(nickChanged) body.nickname = nick;
  if(pw) body.password = pw;
  const { ok, data } = await api('/api/auth/profile', { method:'POST', body });
  if(ok){
    if('nickname' in body) state.nickname = data.nickname || '';
    toast(data.message || '已保存','success');
    closeSheet();
    updateAuthUI();
  }else{
    toast(data.error || '保存失败','error');
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
  state.nickname = '';
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
  await loadNapConfig();
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
  $('cfg-toggle-nap').classList.toggle('on', !!(state.napConfig && state.napConfig.auto_daily));

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
      <select class="time-input" data-field="start" ${disabled?'disabled':''} style="padding:4px 8px;font-size:13px">${timeOptionsHtml(min, max, s)}</select>
      <span class="to">→</span>
      <select class="time-input" data-field="end" ${disabled?'disabled':''} style="padding:4px 8px;font-size:13px">${timeOptionsHtml(min, max, e)}</select>
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
      <select class="time-input" data-field="start">${timeOptionsHtml(TIME_MIN, TIME_MAX_DEFAULT, s)}</select>
      <span class="to">→</span>
      <select class="time-input" data-field="end">${timeOptionsHtml(TIME_MIN, TIME_MAX_DEFAULT, e)}</select>
      <button type="button" class="btn sm ghost seg-del" onclick="removeSimpleSeg(this)">×</button>
    </div>`;
}

function renderCfgSeats(){
  const list = $('cfg-seats');
  list.innerHTML = '';
  const seats = (state.currentCfg && state.currentCfg.seat_list) || [];
  seats.forEach((s, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip draggable';
    chip.dataset.idx = i;
    chip.innerHTML = `<span class="grip">⋮⋮</span><span class="ord">${i+1}</span>${escHtml(s)}<span class="x" onclick="removeCfgSeat(${i})">×</span>`;
    chip.addEventListener('pointerdown', onSeatPointerDown);
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

// ---------- seat priority drag-to-reorder ----------
let _seatDrag = null;

function onSeatPointerDown(e){
  if(e.target && e.target.classList && e.target.classList.contains('x')) return;
  if(e.pointerType === 'mouse' && e.button !== 0) return;
  const chip = e.currentTarget;
  _seatDrag = {
    startX: e.clientX, startY: e.clientY,
    chip, list: chip.parentElement,
    idx: parseInt(chip.dataset.idx, 10),
    overIdx: null,
    started: false,
    pointerId: e.pointerId,
  };
  document.addEventListener('pointermove', onSeatPointerMove);
  document.addEventListener('pointerup', onSeatPointerUp);
  document.addEventListener('pointercancel', onSeatPointerUp);
}

function onSeatPointerMove(e){
  if(!_seatDrag || e.pointerId !== _seatDrag.pointerId) return;
  const dx = e.clientX - _seatDrag.startX;
  const dy = e.clientY - _seatDrag.startY;
  if(!_seatDrag.started){
    if(Math.hypot(dx, dy) < 5) return;
    _seatDrag.started = true;
    _seatDrag.chip.classList.add('dragging');
    try{ _seatDrag.chip.setPointerCapture(_seatDrag.pointerId); }catch(_){}
  }
  if(e.cancelable) e.preventDefault();
  // chip 跟随指针，避免遮挡命中检测
  _seatDrag.chip.style.transform = `translate(${dx}px, ${dy}px) scale(1.05)`;
  _seatDrag.chip.style.pointerEvents = 'none';
  const els = document.elementsFromPoint(e.clientX, e.clientY);
  let over = null;
  for(const el of els){
    if(el.classList && el.classList.contains('chip') && el.parentElement === _seatDrag.list && el !== _seatDrag.chip && !el.classList.contains('add')){
      over = el; break;
    }
  }
  _seatDrag.list.querySelectorAll('.chip.drag-over').forEach(c => c.classList.remove('drag-over'));
  if(over){
    over.classList.add('drag-over');
    _seatDrag.overIdx = parseInt(over.dataset.idx, 10);
  }else{
    _seatDrag.overIdx = null;
  }
}

function onSeatPointerUp(e){
  if(!_seatDrag || e.pointerId !== _seatDrag.pointerId) return;
  document.removeEventListener('pointermove', onSeatPointerMove);
  document.removeEventListener('pointerup', onSeatPointerUp);
  document.removeEventListener('pointercancel', onSeatPointerUp);
  const { chip, started, idx, overIdx, list } = _seatDrag;
  chip.classList.remove('dragging');
  chip.style.transform = '';
  chip.style.pointerEvents = '';
  list.querySelectorAll('.chip.drag-over').forEach(c => c.classList.remove('drag-over'));
  _seatDrag = null;
  if(!started || overIdx === null || overIdx === idx) return;
  const arr = (state.currentCfg && state.currentCfg.seat_list) || [];
  if(idx < 0 || idx >= arr.length || overIdx < 0 || overIdx >= arr.length) return;
  const [moved] = arr.splice(idx, 1);
  arr.splice(overIdx, 0, moved);
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
  if(ok){
    const napAuto = $('cfg-toggle-nap').classList.contains('on');
    if(!state.napConfig || state.napConfig.auto_daily !== napAuto){
      const newNap = { ...(state.napConfig || {}), auto_daily: napAuto };
      await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/nap_config`,
        { method:'POST', body: newNap });
      state.napConfig = newNap;
    }
  }
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

// ---------- reserve-now sheet ----------
let _rnSeats = null;

async function openReserveNow(defaultSeat){
  if(!state.currentPid){ toast('请先添加学号','error'); return; }
  if(!_rnSeats){
    const { ok, data } = await api('/api/public/seats');
    if(!ok || !data.seats){ toast('加载座位列表失败','error'); return; }
    _rnSeats = data.seats;
  }
  openSheet('reserve-now');
  const locSel = $('rn-location');
  if(locSel){
    locSel.innerHTML = '<option value="">选择区域</option>' +
      Object.keys(_rnSeats).map(loc => `<option value="${escHtml(loc)}">${escHtml(loc)}</option>`).join('');
  }
  const seatList = (state.currentCfg && state.currentCfg.seat_list) || [];
  const pick = defaultSeat || seatList[0];
  if(pick) selectRnSeat(pick);
}

function selectRnSeat(seatName){
  if(!_rnSeats || !seatName) return;
  let zone = '';
  for(const [loc, seats] of Object.entries(_rnSeats)){
    if(seats.includes(seatName)){ zone = loc; break; }
  }
  if(!zone){ toast(`未找到「${seatName}」所在区域`,'error'); return; }
  const locSel = $('rn-location');
  if(!locSel) return;
  locSel.value = zone;
  onRnLocationChange();
  const seatSel = $('rn-seat');
  if(seatSel) seatSel.value = seatName;
}

function onRnLocationChange(){
  const loc = $('rn-location').value;
  const seatSel = $('rn-seat');
  if(!seatSel) return;
  if(!loc || !_rnSeats || !_rnSeats[loc]){
    seatSel.innerHTML = '<option value="">请先选区域</option>';
    return;
  }
  seatSel.innerHTML = _rnSeats[loc].map(s => `<option value="${escHtml(s)}">${escHtml(s)}</option>`).join('');
}

function checkRnDuration(){
  const start = $('rn-start').value;
  const end   = $('rn-end').value;
  const hint  = $('rn-duration-hint');
  if(!start || !end){ hint.style.display='none'; return; }
  const [sh,sm] = start.split(':').map(Number);
  const [eh,em] = end.split(':').map(Number);
  const dur = (eh*60+em) - (sh*60+sm);
  if(dur < 120)       { hint.textContent='最短 2 小时'; hint.style.display=''; }
  else if(dur > 900)  { hint.textContent='最长 15 小时'; hint.style.display=''; }
  else                { hint.style.display='none'; }
}

function showReserveResult(msg, success){
  let body;
  if(success){
    const time = (msg.match(/(\d{2}:\d{2})-(\d{2}:\d{2})/) || []);
    const timeStr = time[1] ? `${time[1]} – ${time[2]}` : '';
    const locMatch = msg.match(/新增成功\s+(.+)$/);
    const loc = locMatch ? locMatch[1].trim() : '';
    body = `
      <div style="text-align:center;font-size:36px;margin:4px 0 12px">✅</div>
      <div class="h2" style="text-align:center;margin-bottom:16px">预约成功</div>
      ${timeStr ? `<div class="row-between box tight ghost"><span class="sub">时段</span><span>${escHtml(timeStr)}</span></div>` : ''}
      ${loc     ? `<div class="row-between box tight ghost mt"><span class="sub">位置</span><span>${escHtml(loc)}</span></div>` : ''}
      <button class="btn accent mt-lg" style="width:100%" onclick="closeSheet()">好的</button>`;
  } else {
    const details = [];
    msg.replace(/\n/g, ' ').split(/(?=\d+F-[A-Z]\d+)/).forEach(chunk => {
      const seat   = (chunk.match(/^(\d+F-[A-Z]\d+)/) || [])[1];
      const reason = (chunk.match(/预约失败[：:]\s*(.+?)(?:\s{2,}|$)/) || [])[1];
      if(seat && reason) details.push({ seat, reason: reason.trim() });
    });
    body = `
      <div style="text-align:center;font-size:36px;margin:4px 0 12px">❌</div>
      <div class="h2" style="text-align:center;margin-bottom:16px">预约失败</div>
      ${details.length ? details.map(d => `
        <div class="box tight ghost mt">
          <div class="row-between"><span class="sub">座位</span><span>${escHtml(d.seat)}</span></div>
          <div class="row-between mt"><span class="sub">原因</span><span style="color:var(--danger);text-align:right;max-width:68%">${escHtml(d.reason)}</span></div>
        </div>`).join('') :
        `<div class="box tight ghost" style="color:var(--ink3);font-size:13px">${escHtml(msg)}</div>`}
      <button class="btn ghost mt-lg" style="width:100%" onclick="closeSheet()">知道了</button>`;
  }
  $('sheet-content').innerHTML = `<div class="grab"></div>${body}`;
  const sc = $('scrim');
  sc.classList.remove('center');
  sc.classList.add('show');
}

async function submitReserveNow(){
  if(!state.currentPid){ toast('请先添加学号','error'); return; }
  const seat  = $('rn-seat').value;
  const start = $('rn-start').value;
  const end   = $('rn-end').value;
  if(!seat)         { toast('请选择座位','error'); return; }
  if(!start||!end)  { toast('请填写时间','error'); return; }
  if(start >= end)  { toast('结束时间必须晚于开始时间','error'); return; }

  toast('正在预约…','info');
  const { ok, data } = await api(
    `/api/my/accounts/${encodeURIComponent(state.currentPid)}/reserve_custom`,
    { method:'POST', body: { seat, start_time: start, end_time: end } }
  );
  if(ok){
    closeSheet();
    loadReservations();
    loadNotices();
    showReserveResult(data.result || (data.success ? '预约成功' : '预约完成'), data.success);
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
    card.innerHTML = `<div class="label">今日座位</div><div class="sub" style="margin-top:8px">查询中，请稍等…</div>`;
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
    const nc = state.napConfig || {};
    const napHint = nc.auto_daily
      ? `<div class="meta-row" style="justify-content:center;margin-top:8px"><span class="pill accent">😴 自动午休已开启 · ${escHtml(nc.trigger_time || '12:00')} 触发</span></div>`
      : '';
    empty.innerHTML = `
      <div class="h2" style="color:var(--ink3)">今日暂无预约</div>
      <div class="sub" style="margin-top:6px">${reserveOn ? '按当前配置今日无座位，或抢座进行中' : '自动预约已暂停'}</div>
      ${napHint}
      <div class="actions" style="justify-content:center">
        <button class="btn accent" onclick="openReserveNow()">⚡ 立即预约</button>
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
  const seatHtml = `${zone ? `<span class="zone">${escHtml(zone)}</span>` : ''}${escHtml(num)}`;
  const timeHtml = `${bhm} — ${ehm}${hours ? ` · ${hours}小时` : ''}`;

  if (resv.resvStatus === 3265) {
    card.style.display = '';
    empty.style.display = 'none';
    card.innerHTML = `
      <div class="stamp">TODAY</div>
      <div class="label">今日座位</div>
      <div class="seat">${seatHtml}</div>
      <div class="time">${timeHtml}</div>
      <div class="h2" style="margin-top:12px">任务完成，该休息了 ☕</div>
      <div class="sub" style="margin-top:4px">今日学习已结束</div>
      <div class="actions" style="justify-content:center;margin-top:12px">
        <button class="btn accent" onclick="openSheet('reserve-today')">我还能学！</button>
      </div>`;
    return;
  }

  if (resv.resvStatus === 3281 || resv.resvStatus === 1169) {
    card.style.display = '';
    empty.style.display = 'none';
    card.innerHTML = `
      <div class="stamp">TODAY</div>
      <div class="label">今日座位</div>
      <div class="seat">${seatHtml}</div>
      <div class="time">${timeHtml}</div>
      <div class="meta-row"><span class="pill warn">⚠ 今日预约已违约</span></div>
      <div class="actions" style="margin-top:10px;justify-content:center">
        <button class="btn sm accent" onclick="openReserveNow('${escHtml(seat)}')">⚡ 再次预约</button>
      </div>`;
    return;
  }

  const status = fmtResvStatus(resv.resvStatus);
  const ACTIVE_STATUSES = [1027, 1093, 3141];
  const canCancel = ACTIVE_STATUSES.includes(resv.resvStatus);
  const lpOn = cfg.late_protection === 'True';
  const arrived = cfg.arrived_date === new Date().toISOString().slice(0,10);
  const impliedArrived = resv.resvStatus === 1093 || resv.resvStatus === 3141;
  const canArrive = ACTIVE_STATUSES.includes(resv.resvStatus);
  const showArrived = arrived || impliedArrived;

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
      ${(state.napConfig || {}).auto_daily ? `<span class="pill accent" style="cursor:pointer" onclick="openNap()">😴 午休 ${escHtml((state.napConfig||{}).trigger_time||'12:00')}</span>` : ''}
      ${showArrived ? '<span class="pill ok">✓ 已到馆</span>' : ''}
    </div>
    <div class="actions">
      ${canArrive ? `<button class="btn ${showArrived?'primary':'accent'} lg grow" id="btn-arrived" onclick="toggleArrived()">${showArrived ? '✓ 已到馆' : '✓ 我已到馆'}</button>` : ''}
      ${canCancel ? `<button class="btn sm" onclick="openNap()">😴 午休</button>` : ''}
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
    card.innerHTML = `<div class="label">明日座位</div><div class="sub" style="margin-top:8px">查询中，请稍等…</div>`;
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
  return { 1027:'已预约', 1093:'使用中', 1169:'已违约', 3141:'暂离', 3265:'已结束', 3281:'已违约' }[s] || `状态(${s})`;
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
  startEl.innerHTML = timeOptionsHtml(min, max, s);
  endEl.innerHTML = timeOptionsHtml(min, max, e);

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

function frequentSeatChips(zoneId, seatId){
  const list = (state.currentCfg && state.currentCfg.seat_list) || [];
  const chips = list.slice(0, 3);
  if(!chips.length) return '';
  return `
    <div class="box tight" style="background:#dbeafe;border:1px solid #93c5fd;margin-bottom:8px">
      <div class="tiny" style="color:#1e40af;margin-bottom:6px;font-weight:600">常用座位 · 点击快速选择</div>
      <div class="row-flex" style="flex-wrap:wrap;gap:6px">
        ${chips.map(s => `<button type="button" class="btn sm" style="background:#bfdbfe;color:#1e3a8a;border:0" onclick="fillSeatPicker('${escHtml(s)}','${zoneId}','${seatId}')">${escHtml(s)}</button>`).join('')}
      </div>
    </div>`;
}

function fillSeatPicker(seatName, zoneId, seatId){
  if(!seatName || !state.allSeats) return;
  let zone = '';
  for(const [z, seats] of Object.entries(state.allSeats)){
    if(seats.includes(seatName)){ zone = z; break; }
  }
  if(!zone){ toast(`未找到「${seatName}」所在区域`,'error'); return; }
  const zSel = $(zoneId);
  if(!zSel) return;
  zSel.value = zone;
  onZone(zone, seatId);
  const sSel = $(seatId);
  if(sSel) sSel.value = seatName;
}

function sortedZones(){
  const ORDER = ['二楼A区','二楼B区','六楼A区','七楼A区','七楼B区','三楼夹层','三楼A区','三楼B区','三楼C区','四楼夹层','四楼A区','五楼A区'];
  const keys = Object.keys(state.allSeats);
  return keys.sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b);
  });
}

function onZone(v, targetId){
  const sel = $(targetId || 'sp-seat');
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
  profile: () => `
    <div class="grab"></div>
    <h3>账号资料</h3>
    <div class="desc">昵称会替代用户名 <strong>${escHtml(state.uid)}</strong> 显示。各字段留空即不修改。</div>
    <div class="col gap-sm">
      <div class="field"><label>昵称（可选）</label><input type="text" id="pf-nick" maxlength="30" placeholder="清空 = 恢复用 ${escHtml(state.uid)}" value="${escHtml(state.nickname || '')}"></div>
      <div class="field"><label>新密码（可选）</label><input type="password" id="pf-pass" placeholder="留空 = 不修改"></div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="saveProfile()">保存</button>
    </div>
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
  'reserve-today': () => {
    const zones = sortedZones();
    const isoDay = new Date().getDay() || 7;
    const { min, max } = timeBounds(isoDay);
    return `
    <div class="grab"></div>
    <h3>再约一场 📚</h3>
    <div class="desc">选好座位和时间，直接预约今天。</div>
    <div class="col gap-sm">
      <div class="field"><label>楼层 / 区域</label>
        <select id="rt-zone" onchange="onZone(this.value,'rt-seat')">
          <option value="">选择...</option>
          ${zones.map(z => `<option value="${escHtml(z)}">${escHtml(z)}</option>`).join('')}
        </select>
      </div>
      <div class="field"><label>座位号</label>
        <select id="rt-seat"><option>先选楼层</option></select>
      </div>
      <div class="time-pair" style="display:flex;gap:8px">
        <div class="field" style="flex:1"><label>开始时间</label>
          <select id="rt-start" class="time-input" data-field="start">${timeOptionsHtml(min, max, min)}</select>
        </div>
        <div class="field" style="flex:1"><label>结束时间</label>
          <select id="rt-end" class="time-input" data-field="end">${timeOptionsHtml(min, max, max)}</select>
        </div>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" id="btn-rt-confirm" onclick="doReserveToday()">确认预约</button>
    </div>`;
  },

  'nap-info': () => `
    <div class="grab"></div>
    <h3>😴 一键午休</h3>
    <div class="desc">专为 2 小时午休设计的快捷功能。</div>
    <div class="col gap-sm">
      <div class="box tight" style="border-left:4px solid var(--accent)">
        <div class="sub" style="font-weight:700">✓ 自动续约下午</div>
        <div class="t">点击后系统立即取消当前预约，并以相同座位重新预约下午时段（默认 14:00 起）</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--ok)">
        <div class="sub" style="font-weight:700">✓ 可自由配置</div>
        <div class="t">在设置页可修改默认下午时间、座位，以及开启每日自动触发</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--danger)">
        <div class="sub" style="font-weight:700">⚠ 极小占座风险</div>
        <div class="t">取消到重新预约约需 1 秒，极低概率被他人抢占。请知悉后再使用</div>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="acknowledgeNap()">我知道了，继续</button>
    </div>
  `,

  'reserve-now': () => {
    const seatList = (state.currentCfg && state.currentCfg.seat_list) || [];
    const chips = seatList.slice(0, 3);
    return `
    <div class="grab"></div>
    <h3>⚡ 立即预约</h3>
    ${chips.length ? `
      <div class="box tight mt" style="background:#dbeafe;border:1px solid #93c5fd">
        <div class="tiny" style="color:#1e40af;margin-bottom:6px;font-weight:600">常用座位 · 点击快速选择</div>
        <div class="row-flex" style="flex-wrap:wrap;gap:6px">
          ${chips.map(s => `<button class="btn sm" style="background:#bfdbfe;color:#1e3a8a;border:0" onclick="selectRnSeat('${escHtml(s)}')">${escHtml(s)}</button>`).join('')}
        </div>
      </div>
    ` : ''}
    <div class="col gap-sm mt">
      <div class="field">
        <label>区域</label>
        <select id="rn-location" onchange="onRnLocationChange()">
          <option value="">加载中…</option>
        </select>
      </div>
      <div class="field">
        <label>座位</label>
        <select id="rn-seat">
          <option value="">请先选区域</option>
        </select>
      </div>
      <div class="row-flex mt">
        <div class="field">
          <label>开始</label>
          <input type="time" id="rn-start" value="08:00" oninput="checkRnDuration()">
        </div>
        <div class="field">
          <label>结束</label>
          <input type="time" id="rn-end" value="22:00" oninput="checkRnDuration()">
        </div>
      </div>
      <div class="tiny" id="rn-duration-hint" style="color:var(--warn);display:none"></div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="submitReserveNow()">确认预约</button>
    </div>`;
  },

  'nap-about': () => `
    <div class="grab"></div>
    <h3>😴 一键午休</h3>
    <div class="desc">专为午休设计的快捷功能，出门吃饭前点一下，回来时座位还在。</div>
    <div class="col gap-sm">
      <div class="box tight" style="border-left:4px solid var(--accent)">
        <div class="sub" style="font-weight:700">✓ 自动续约下午</div>
        <div class="t">系统立即取消当前预约，并以相同座位重新预约下午时段（默认 14:00 起）</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--ok)">
        <div class="sub" style="font-weight:700">✓ 每日自动触发</div>
        <div class="t">在设置页开启后，每天到触发时刻（默认 12:00）自动执行，无需手动操作</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--danger)">
        <div class="sub" style="font-weight:700">⚠ 极小占座风险</div>
        <div class="t">取消到重新预约约需 1 秒，极低概率被他人抢占</div>
      </div>
    </div>
    <button class="btn primary mt-lg" style="width:100%" onclick="closeSheet()">好的</button>
  `,

  'nap-confirm': () => {
    const resv = state.todayResv;
    const currentSeat = (resv && resv.devInfo && resv.devInfo.devName) || '';
    const nc = state.napConfig || {};
    const defStart = nc.start_time || '14:00';
    const defSeat = nc.seat || '';
    const isoDay = new Date().getDay() || 7;
    const { max } = timeBounds(isoDay);
    const zones = sortedZones();
    return `
    <div class="grab"></div>
    <h3>😴 午休设置</h3>
    <div class="desc">当前座位：<strong>${escHtml(currentSeat || '未知')}</strong></div>
    <div class="col gap-sm">
      <div class="field"><label>午休结束（回来时间）</label>
        <select id="nap-start" class="time-input">${timeOptionsHtml('08:00', max, defStart)}</select>
      </div>
      <div class="field">
        <label>座位</label>
        <select id="nap-seat-mode" onchange="onNapSeatMode(this.value)">
          <option value="same"${!defSeat ? ' selected' : ''}>当前座位（${escHtml(currentSeat || '自动')}）</option>
          <option value="custom"${defSeat ? ' selected' : ''}>自定义座位</option>
        </select>
      </div>
      <div id="nap-custom-seat" style="display:${defSeat ? 'block' : 'none'}">
        ${frequentSeatChips('nap-zone','nap-seat-pick')}
        <div class="col gap-sm">
          <div class="field"><label>楼层 / 区域</label>
            <select id="nap-zone" onchange="onZone(this.value,'nap-seat-pick')">
              <option value="">选择...</option>
              ${zones.map(z => `<option value="${escHtml(z)}">${escHtml(z)}</option>`).join('')}
            </select>
          </div>
          <div class="field"><label>座位号</label>
            <select id="nap-seat-pick"><option value="">先选楼层</option></select>
          </div>
        </div>
      </div>
    </div>
    <div class="row-flex mt-lg" style="gap:8px">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn primary grow" onclick="saveNapConfig(false)">仅保存</button>
      <button class="btn accent grow" id="btn-nap-confirm" onclick="doNap()">确认午休</button>
    </div>`;
  },

  'nap-settings': () => {
    const nc = state.napConfig || {};
    const defStart = nc.start_time || '14:00';
    const defSeat = nc.seat || '';
    const autoDaily = nc.auto_daily || false;
    const triggerTime = nc.trigger_time || '12:00';
    const isoDay = new Date().getDay() || 7;
    const { max } = timeBounds(isoDay);
    const zones = sortedZones();
    return `
    <div class="grab"></div>
    <h3>😴 午休配置</h3>
    <div class="desc">默认 12:00 出门，14:00 回来。下午时段自动续约到原预约结束。</div>
    <div class="col gap-sm">
      <div class="time-pair" style="display:flex;gap:8px">
        <div class="field" style="flex:1"><label>午休开始</label>
          <select id="ns-trigger" class="time-input">${timeOptionsHtml('08:00', '22:00', triggerTime)}</select>
        </div>
        <div class="field" style="flex:1"><label>午休结束</label>
          <select id="ns-start" class="time-input">${timeOptionsHtml('08:00', max, defStart)}</select>
        </div>
      </div>
      <div class="field">
        <label>默认座位</label>
        <select id="ns-seat-mode" onchange="onNapSettingsSeatMode(this.value)">
          <option value="same"${!defSeat ? ' selected' : ''}>当前预约的座位（自动）</option>
          <option value="custom"${defSeat ? ' selected' : ''}>固定自定义座位</option>
        </select>
      </div>
      <div id="ns-custom-seat" style="display:${defSeat ? 'block' : 'none'}">
        ${frequentSeatChips('ns-zone','ns-seat-pick')}
        <div class="col gap-sm">
          <div class="field"><label>楼层 / 区域</label>
            <select id="ns-zone" onchange="onZone(this.value,'ns-seat-pick')">
              <option value="">选择...</option>
              ${zones.map(z => `<option value="${escHtml(z)}">${escHtml(z)}</option>`).join('')}
            </select>
          </div>
          <div class="field"><label>座位号</label>
            <select id="ns-seat-pick"><option value="">先选楼层</option></select>
          </div>
        </div>
      </div>
      <div class="toggle-row" style="border:0;padding:4px 0">
        <div class="info">
          <div class="t1">每日自动午休</div>
          <div class="t2">每天到「午休开始」时刻自动执行</div>
        </div>
        <label class="switch"><input type="checkbox" id="ns-auto-daily" ${autoDaily ? 'checked' : ''}><span class="slider"></span></label>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="saveNapSettings()">保存</button>
    </div>`;
  },
};

// ========== 一键午休 ==========

async function loadNapConfig(){
  if(!state.currentPid) return;
  try{
    const { ok, data } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/nap_config`);
    if(ok) state.napConfig = data;
  }catch(e){}
  updateNapSettingsLabel();
}

function updateNapSettingsLabel(){
  const el = $('nap-settings-label');
  if(!el) return;
  const nc = state.napConfig || {};
  const seat = nc.seat ? nc.seat : '同座位';
  const trigger = nc.trigger_time || '12:00';
  const start = nc.start_time || '14:00';
  const auto = nc.auto_daily ? '，每日自动' : '';
  el.textContent = `午休 ${trigger}–${start}，${seat}${auto}`;
}

function openNap(){
  if(!state.todayResv){ toast('今日无预约','info'); return; }
  if(!localStorage.getItem('autolib_nap_ack')){
    openSheet('nap-info');
  }else{
    openSheet('nap-confirm');
  }
}

function acknowledgeNap(){
  localStorage.setItem('autolib_nap_ack','1');
  closeSheet();
  setTimeout(() => openSheet('nap-confirm'), 100);
}

function onNapSeatMode(val){
  const el = $('nap-custom-seat');
  if(el) el.style.display = val === 'custom' ? 'block' : 'none';
}

function onNapSettingsSeatMode(val){
  const el = $('ns-custom-seat');
  if(el) el.style.display = val === 'custom' ? 'block' : 'none';
}

async function saveNapConfig(closeAfter=true){
  const start = ($('nap-start') || {}).value;
  const seatMode = ($('nap-seat-mode') || {}).value;
  let seat = '';
  if(seatMode === 'custom'){
    const seatEl = $('nap-seat-pick');
    seat = seatEl ? (seatEl.options[seatEl.selectedIndex]?.value || '') : '';
    if(!seat || seat === '先选楼层'){ toast('请先选择自定义座位','error'); return; }
  }
  const cfg = { ...state.napConfig, start_time: start, end_time: '', seat };
  const { ok } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/nap_config`,
    { method:'POST', body: cfg });
  if(ok){
    state.napConfig = cfg;
    updateNapSettingsLabel();
    toast('午休配置已保存','success');
    if(closeAfter) closeSheet();
  }else{
    toast('保存失败','error');
  }
}

async function saveNapSettings(){
  const start = ($('ns-start') || {}).value || '14:00';
  const triggerTime = ($('ns-trigger') || {}).value || '12:00';
  if(triggerTime >= start){ toast('午休结束必须晚于开始','error'); return; }
  const seatMode = ($('ns-seat-mode') || {}).value;
  let seat = '';
  if(seatMode === 'custom'){
    const seatEl = $('ns-seat-pick');
    seat = seatEl ? (seatEl.options[seatEl.selectedIndex]?.value || '') : '';
    if(!seat || seat === '先选楼层'){ toast('请先选择自定义座位','error'); return; }
  }
  const autoDaily = ($('ns-auto-daily') || {}).checked || false;
  const cfg = { start_time: start, end_time: '', seat, auto_daily: autoDaily, trigger_time: triggerTime };
  const { ok } = await api(`/api/my/accounts/${encodeURIComponent(state.currentPid)}/nap_config`,
    { method:'POST', body: cfg });
  if(ok){
    state.napConfig = cfg;
    updateNapSettingsLabel();
    toast('午休配置已保存','success');
    closeSheet();
  }else{
    toast('保存失败','error');
  }
}

async function doNap(){
  if(!state.todayResv){ toast('今日无预约','info'); return; }
  const start = ($('nap-start') || {}).value;
  // end 继承当前预约结束时间
  const end = ((state.todayResv.resvEndTime || '').split(' ')[1] || '').slice(0,5);
  if(!start || !end){ toast('请检查时间','error'); return; }
  if(start >= end){ toast('回来时间晚于原预约结束','error'); return; }

  const seatMode = ($('nap-seat-mode') || {}).value;
  let seat = '';
  if(seatMode === 'custom'){
    const seatEl = $('nap-seat-pick');
    seat = seatEl ? (seatEl.options[seatEl.selectedIndex]?.value || '') : '';
    if(!seat || seat === '先选楼层'){ toast('请先选择自定义座位','error'); return; }
  }else{
    seat = (state.todayResv.devInfo && state.todayResv.devInfo.devName) || '';
  }
  if(!seat){ toast('无法获取座位信息','error'); return; }

  const btn = $('btn-nap-confirm');
  if(btn) btn.disabled = true;
  closeSheet();

  toast('取消中…','info');
  const { ok, data } = await api(
    `/api/my/accounts/${encodeURIComponent(state.currentPid)}/nap`,
    { method:'POST', body: { uuid: state.todayResv.uuid, seat, start_time: start, end_time: end } }
  );
  if(btn) btn.disabled = false;

  if(!ok){
    toast(data.error || '午休失败','error');
    return;
  }
  if(!data.cancel_success){
    toast(data.error || '取消失败','error');
    return;
  }
  if(data.success){
    toast('午休成功 😴 下午见！','success');
  }else{
    toast('取消成功，但重新预约失败，请手动预约下午时段','error');
    if(data.result) alert('重新预约结果：\n\n' + data.result);
  }
  state.todayResv = null;
  loadReservations();
}

// ========== end 一键午休 ==========

async function doReserveToday(){
  const zone = ($('rt-zone') || {}).value;
  const seatEl = $('rt-seat');
  const seat = seatEl ? seatEl.options[seatEl.selectedIndex]?.value : '';
  const start = ($('rt-start') || {}).value;
  const end = ($('rt-end') || {}).value;

  if(!zone || !seat || seat === '先选楼层') { toast('请先选择座位','error'); return; }
  if(!start || !end || start >= end) { toast('请检查时间段','error'); return; }

  const fullSeat = seat;
  const btn = $('btn-rt-confirm');
  if(btn) btn.disabled = true;

  const { ok, data } = await api(
    `/api/my/accounts/${encodeURIComponent(state.currentPid)}/reserve_custom`,
    { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ seat: fullSeat, start_time: start, end_time: end }) }
  );

  if(btn) btn.disabled = false;

  if(ok){
    closeSheet();
    toast(data.success ? '预约成功 🎉' : (data.result || '预约完成'), data.success ? 'success' : 'info');
    loadReservations();
    if(data.result && !data.success) alert('预约结果：\n\n' + data.result);
  } else {
    toast(data.error || '预约失败','error');
  }
}

function openSheet(name){
  const sc = $('scrim');
  const content = $('sheet-content');
  const tpl = SHEETS[name];
  content.innerHTML = tpl ? tpl() : '<div class="grab"></div><h3>未实现</h3>';
  sc.classList.add('show');
  if(name === 'lp-info' || name === 'lp-warning' || name === 'cancel' || name === 'guest-data-notice' || name === 'nap-info' || name === 'nap-about') sc.classList.add('center');
  else sc.classList.remove('center');
}
function closeSheet(){ $('scrim').classList.remove('show'); }

// ---------- home loader ----------
async function loadHome(){
  renderHome();
  loadNotices();
}

// 起止联动：开始时间变化时，重建结束下拉，只保留 > 开始 的档位
document.addEventListener('change', (e) => {
  const el = e.target;
  if(!el || !el.matches) return;
  const isStart = el.matches('select.time-input[data-field="start"]') || el.id === 'tmr-start';
  if(!isStart) return;
  const container = el.closest('.seg-row, .time-pair');
  if(!container) return;
  const endEl = container.querySelector('select.time-input[data-field="end"], #tmr-end');
  if(!endEl) return;
  const startVal = el.value;
  const allOpts = Array.from(el.options).map(o => o.value);
  const endOpts = allOpts.filter(v => v > startVal);
  if(!endOpts.length) return;
  const currentEnd = endEl.value;
  const newEnd = endOpts.includes(currentEnd) && currentEnd > startVal ? currentEnd : endOpts[0];
  endEl.innerHTML = endOpts.map(o => `<option value="${o}"${o===newEnd?' selected':''}>${o}</option>`).join('');
});

// ---------- init ----------
async function init(){
  // Wire scrim click-outside
  $('scrim').addEventListener('click', e => { if(e.target.id === 'scrim') closeSheet(); });

  await checkAuth();
  await loadSeats();
  await loadAccounts();
  loadNotices();
}

init();
