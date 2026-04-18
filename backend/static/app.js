// ============ AutoLib · 手绘风 UI · 前端逻辑 ============

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accent":"#ff5c3a",
  "density":"normal",
  "wobble":true,
  "font":"Gaegu"
}/*EDITMODE-END*/;

const state = {
  page: 'home',
  uid: null,
  authed: false,
  accounts: [],
  currentPid: null,
  currentCfg: null,
  allSeats: {},
  cfgSeats: [],
  tmrOpen: false,
  arrived: false,
  noticesCollapsed: false,
  cfgMode: 'week',
  tweaks: Object.assign({}, TWEAK_DEFAULTS)
};

// ===== Helpers =====
function esc(s){ return (s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function toMin(t){ const [h,m]=(t||'00:00').split(':'); return parseInt(h)*60+parseInt(m||0); }

// ===== Toast =====
function toast(msg, type='info'){
  const box=document.getElementById('toasts');
  const el=document.createElement('div');
  el.className='toast '+(type==='success'?'ok':type==='error'?'err':'');
  el.textContent=msg;
  box.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; setTimeout(()=>el.remove(),300); },2800);
}

// ===== Nav =====
function go(p){
  state.page=p;
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('show'));
  document.getElementById('page-'+p).classList.add('show');
  document.querySelectorAll('.bnav > button').forEach(b=>{
    b.classList.toggle('on',b.dataset.tab===p);
  });
  if(p==='config') loadConfigPage();
  if(p==='home') loadNotices();
  if(p==='settings') renderSettingsPage();
  window.scrollTo({top:0,behavior:'smooth'});
}

// ===== Auth =====
async function checkAuth(){
  try {
    const r=await fetch('/api/auth/me');
    const d=await r.json();
    if(d.logged_in){ state.uid=d.uid; state.authed=true; onLoggedIn(); }
    // 未登录时不强制弹窗，允许游客浏览
  } catch(e){}
}

let _authMode='login';
function authSwitchMode(mode, btn){
  _authMode=mode;
  document.querySelectorAll('#auth-seg button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  const sb=document.getElementById('auth-submit');
  if(sb) sb.textContent=mode==='login'?'登录':'注册';
}

async function doAuth(mode){
  const uid=(document.getElementById('auth-uid').value||'').trim();
  const pass=document.getElementById('auth-pass').value||'';
  if(!uid||!pass){ toast('请填写用户名和密码','error'); return; }
  const btn=document.getElementById('auth-submit');
  btn.disabled=true; btn.textContent='...';
  try {
    const r=await fetch(`/api/auth/${mode}`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:uid, password:pass})
    });
    const d=await r.json();
    if(r.ok){
      toast(d.message,'success');
      state.uid=uid; state.authed=true;
      closeSheet();
      onLoggedIn();
    } else { toast(d.error,'error'); }
  } catch(e){ toast('网络错误','error'); }
  finally { btn.disabled=false; btn.textContent=mode==='login'?'登录':'注册'; }
}

async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'}).catch(()=>{});
  state.uid=null; state.authed=false;
  state.accounts=[]; state.currentPid=null; state.currentCfg=null;
  // 重置顶栏与点击行为
  const hello=document.getElementById('hello'); if(hello) hello.textContent='你好，访客 ☕';
  const pidText=document.getElementById('pid-text'); if(pidText) pidText.textContent='未登录';
  const pidDot=document.getElementById('pid-dot'); if(pidDot) pidDot.className='dot off';
  const chip=document.getElementById('pid-chip');
  if(chip) chip.setAttribute('onclick',"openSheet('auth')");
  // 重置主页/明日条/星期条
  renderHomeCard();
  document.querySelectorAll('#page-home .week .row .track').forEach(t=>{ t.innerHTML=''; });
  const ss=document.querySelector('#tmr-strip .info .ss');
  if(ss) ss.textContent='登录后查看明日预约信息';
  renderNotices([],[]);
  go('home');
  openSheet('auth');
}

function onLoggedIn(){
  document.getElementById('hello').textContent=`你好，${state.uid} ☕`;
  // pid-chip 登录后改为打开账号列表
  const chip=document.getElementById('pid-chip');
  if(chip) chip.setAttribute('onclick',"openSheet('accounts')");
  loadSeats();
  loadAccounts().then(()=>{
    if(state.accounts.length){
      selectAccount(state.accounts[0].pid, false);
    } else {
      const pidText=document.getElementById('pid-text');
      if(pidText) pidText.textContent='无账号';
      renderHomeCard();
    }
    loadNotices();
    if(state.page==='settings') renderSettingsPage();
  });
}

// ===== Accounts =====
async function loadAccounts(){
  try {
    const r=await fetch('/api/my/accounts');
    if(r.status===401){
      state.accounts=[]; state.authed=false;
      openSheet('auth'); return;
    }
    state.accounts=await r.json();
  } catch(e){ toast('加载账号失败','error'); }
}

async function selectAccount(pid, closeSheetAfter=true){
  state.currentPid=pid;
  document.getElementById('pid-text').textContent=pid;
  if(closeSheetAfter) closeSheet();
  try {
    const r=await fetch(`/api/my/accounts/${pid}`);
    const cfg=await r.json();
    state.currentCfg=cfg;
    const dot=document.getElementById('pid-dot');
    if(dot) dot.className='dot'+(cfg.is_reserved==='True'?'':' off');
    renderHomeCard();
    renderWeekBars();
    updateTmrStrip();
  } catch(e){}
}

async function deleteAccount(pid){
  if(!confirm(`确定删除学号 ${pid} 的所有配置？`)) return;
  try {
    const r=await fetch(`/api/my/accounts/${pid}`,{method:'DELETE'});
    const d=await r.json();
    if(r.ok){
      toast(d.message,'success');
      await loadAccounts();
      if(state.currentPid===pid){
        state.currentPid=null; state.currentCfg=null;
        if(state.accounts.length) selectAccount(state.accounts[0].pid,false);
        else { document.getElementById('pid-text').textContent='无账号'; renderHomeCard(); }
      }
      openSheet('accounts');
    } else { toast(d.error||'删除失败','error'); }
  } catch(e){ toast('网络错误','error'); }
}

// ===== Add account =====
async function verifyAndAdd(){
  const pid=(document.getElementById('add-pid-input').value||'').trim();
  const vpn=document.getElementById('add-vpn').value||'';
  const lib=document.getElementById('add-lib').value||'';
  if(!pid||!vpn||!lib){ toast('请填写完整','error'); return; }
  const btn=document.getElementById('add-submit');
  btn.disabled=true; btn.textContent='验证中...';
  try {
    const vr=await fetch(`/api/my/accounts/${pid}/verify`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({vpn_password:vpn, lib_password:lib})
    });
    const vd=await vr.json();
    if(!vd.verified){ toast(vd.error||'验证失败','error'); return; }
    toast('验证通过，正在保存...','success');
    btn.textContent='保存中...';
    const sr=await fetch(`/api/my/accounts/${pid}`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({vpn_password:vpn,lib_password:lib,
        is_reserved:'False',late_protection:'True',mode:'week_time',verified:true})
    });
    if(sr.ok){
      await loadAccounts();
      selectAccount(pid,true);
      toast(`学号 ${pid} 已添加`,'success');
    } else {
      const sd=await sr.json();
      toast(sd.error||'保存失败','error');
    }
  } catch(e){ toast('网络错误','error'); }
  finally { btn.disabled=false; btn.textContent='验证并保存'; }
}

// ===== Config page =====
async function loadConfigPage(){
  if(!state.currentPid) return;
  try {
    const r=await fetch(`/api/my/accounts/${state.currentPid}`);
    const cfg=await r.json();
    if(!cfg.pid) return;
    state.currentCfg=cfg;
    renderConfigForm(cfg);
  } catch(e){ toast('加载配置失败','error'); }
}

function renderConfigForm(cfg){
  const backendMode=cfg.mode||'week_time';
  state.cfgMode=backendMode==='week_time'?'week':'simple';
  const modeButtons=document.querySelectorAll('#page-config .seg > button');
  modeButtons.forEach(b=>b.classList.remove('on'));
  modeButtons[state.cfgMode==='week'?0:1]?.classList.add('on');
  document.getElementById('week-mode').style.display=state.cfgMode==='week'?'':'none';
  document.getElementById('simple-mode').style.display=state.cfgMode==='simple'?'':'none';

  state.cfgSeats=(cfg.seat_list||[]).slice();
  renderCfgSeats();

  const rEl=document.getElementById('cfg-reserved');
  const lEl=document.getElementById('cfg-late');
  if(rEl) rEl.classList.toggle('on',cfg.is_reserved!=='False');
  if(lEl) lEl.classList.toggle('on',cfg.late_protection!=='False');

  const vpnEl=document.getElementById('cfg-vpn');
  const libEl=document.getElementById('cfg-lib');
  if(vpnEl) vpnEl.value=cfg.vpn_password||'';
  if(libEl) libEl.value=cfg.lib_password||'';

  if(state.cfgMode==='week' && cfg.time?.week_time){
    for(let i=1;i<=7;i++){
      const t=cfg.time.week_time[String(i)]||'08:00-22:00';
      const [s,e]=t.split('-');
      const rowEl=document.querySelector(`.week-row-${i}`);
      if(!rowEl) continue;
      const sEl=rowEl.querySelector('input[data-role=start]');
      const eEl=rowEl.querySelector('input[data-role=end]');
      if(sEl) sEl.value=s||'08:00';
      if(eEl) eEl.value=e||'22:00';
    }
  } else if(state.cfgMode==='simple' && cfg.time?.tomorrow){
    const [s,e]=cfg.time.tomorrow.split('-');
    const sEl=document.getElementById('cfg-simple-start');
    const eEl=document.getElementById('cfg-simple-end');
    if(sEl) sEl.value=s||'08:00';
    if(eEl) eEl.value=e||'22:00';
  }
}

function renderCfgSeats(){
  const list=document.getElementById('cfg-seats');
  if(!list) return;
  const chips=state.cfgSeats.map((s,i)=>
    `<span class="chip"><span class="ord">${i+1}</span>${esc(s)}<span class="x" onclick="removeCfgSeat(${i})">×</span></span>`
  ).join('');
  list.innerHTML=chips+`<span class="chip add" onclick="openSheet('seat-picker')">+ 加座位</span>`;
}

function removeCfgSeat(i){ state.cfgSeats.splice(i,1); renderCfgSeats(); }

function setMode(m, e){
  state.cfgMode=m;
  document.querySelectorAll('#page-config .seg > button').forEach(b=>b.classList.remove('on'));
  e.currentTarget.classList.add('on');
  document.getElementById('week-mode').style.display=m==='week'?'':'none';
  document.getElementById('simple-mode').style.display=m==='simple'?'':'none';
}

function buildWeekGrid(){
  const days=['一','二','三','四','五','六','日'];
  const defaults=['08:00-22:00','08:00-22:00','08:00-22:00','08:00-22:00','08:00-20:00','08:00-22:00','10:00-18:00'];
  const grid=document.getElementById('week-grid');
  if(!grid) return;
  grid.innerHTML='';
  days.forEach((d,i)=>{
    const [s,e]=defaults[i].split('-');
    const row=document.createElement('div');
    row.className=`box tight week-row-${i+1}`;
    row.innerHTML=`
      <div class="row-between">
        <div class="row-flex" style="gap:8px">
          <div class="toggle on" onclick="tToggle(this,event)"></div>
          <div class="sub" style="font-weight:700">周${d}</div>
        </div>
        <div class="time-pair" style="flex:0 0 auto;max-width:220px">
          <input class="time-input" type="time" value="${s}" data-role="start" style="padding:4px 8px;font-size:13px">
          <span class="to">→</span>
          <input class="time-input" type="time" value="${e}" data-role="end" style="padding:4px 8px;font-size:13px">
        </div>
      </div>`;
    grid.appendChild(row);
  });
}

async function saveCfg(){
  if(!state.currentPid){ toast('请先选择学号','error'); return; }
  const backendMode=state.cfgMode==='week'?'week_time':'tomorrow';
  let timeConfig={};
  if(state.cfgMode==='week'){
    const wt={};
    for(let i=1;i<=7;i++){
      const rowEl=document.querySelector(`.week-row-${i}`);
      if(!rowEl) continue;
      const s=(rowEl.querySelector('input[data-role=start]')||{}).value||'08:00';
      const e=(rowEl.querySelector('input[data-role=end]')||{}).value||'22:00';
      wt[String(i)]=`${s}-${e}`;
    }
    timeConfig={week_time:wt};
  } else {
    const s=(document.getElementById('cfg-simple-start')||{}).value||'08:00';
    const e=(document.getElementById('cfg-simple-end')||{}).value||'22:00';
    timeConfig={tomorrow:`${s}-${e}`};
  }

  const isReserved=document.getElementById('cfg-reserved')?.classList.contains('on')?'True':'False';
  const lateProtection=document.getElementById('cfg-late')?.classList.contains('on')?'True':'False';
  const vpnPwd=document.getElementById('cfg-vpn')?.value||'';
  const libPwd=document.getElementById('cfg-lib')?.value||'';

  const body={
    vpn_password:vpnPwd, lib_password:libPwd,
    seat_list:state.cfgSeats, mode:backendMode, time:timeConfig,
    is_reserved:isReserved, late_protection:lateProtection
  };

  const btn=document.querySelector('#page-config button.accent');
  if(btn){ btn.disabled=true; btn.textContent='保存中...'; }
  try {
    const r=await fetch(`/api/my/accounts/${state.currentPid}`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    });
    if(r.ok){
      toast('配置已保存','success');
      state.currentCfg=Object.assign(state.currentCfg||{},body,{seat_list:state.cfgSeats.slice()});
      await loadAccounts();
      renderWeekBars(); renderHomeCard(); updateTmrStrip();
      setTimeout(()=>go('home'),400);
    } else {
      const d=await r.json();
      toast(d.error||'保存失败','error');
    }
  } catch(e){ toast('网络错误','error'); }
  finally { if(btn){ btn.disabled=false; btn.textContent='保存配置'; } }
}

// ===== Home card =====
function renderHomeCard(){
  const cfg=state.currentCfg;
  const todayCard=document.getElementById('today-card');
  const emptyCard=document.getElementById('today-empty');
  if(!todayCard||!emptyCard) return;

  if(!cfg?.pid || cfg.is_reserved==='False' || !(cfg.seat_list||[]).length){
    todayCard.style.display='none'; emptyCard.style.display=''; return;
  }

  const seat=cfg.seat_list[0];
  const todayN=new Date().getDay()||7; // 1=Mon...7=Sun
  let s='08:00', e='22:00';
  if(cfg.mode==='week_time' && cfg.time?.week_time){
    const t=cfg.time.week_time[String(todayN)];
    if(t)[s,e]=t.split('-');
  } else if(cfg.time?.tomorrow){ [s,e]=cfg.time.tomorrow.split('-'); }
  const durH=Math.round((toMin(e)-toMin(s))/60);

  const m=seat.match(/^([A-Za-z]+\d*F?)(.+)$/)||seat.match(/^([A-Za-z]+)(\d{3,})$/);
  const zone=m?m[1]:'', seatNum=m?m[2]:seat;
  todayCard.querySelector('.seat').innerHTML=
    zone?`<span class="zone">${esc(zone)}</span>${esc(seatNum)}`:esc(seat);
  todayCard.querySelector('.time').textContent=`${s} — ${e} · ${durH}小时`;

  const result=cfg.result||'';
  const pillOk=todayCard.querySelector('.pill.ok');
  if(pillOk){
    if(result.includes('成功')) pillOk.innerHTML='<span class="dot"></span>已预约';
    else if(result) pillOk.innerHTML=`<span class="dot"></span>${esc(result.slice(0,8))}`;
    else pillOk.innerHTML='<span class="dot"></span>待预约';
  }
  todayCard.style.display=''; emptyCard.style.display='none';
}

// ===== Week bars (home) =====
function renderWeekBars(){
  const cfg=state.currentCfg;
  const rows=document.querySelectorAll('#page-home .week .row');
  const todayIdx=(new Date().getDay()+6)%7; // 0=Mon
  const tmrIdx=(todayIdx+1)%7;
  const TOTAL=(22-6)*60;

  rows.forEach((row,i)=>{
    const track=row.querySelector('.track');
    if(!track) return;
    let s='08:00', e='22:00';
    if(cfg?.mode==='week_time' && cfg.time?.week_time?.[String(i+1)]){
      [s,e]=cfg.time.week_time[String(i+1)].split('-');
    } else if(cfg?.time?.tomorrow){ [s,e]=cfg.time.tomorrow.split('-'); }
    const left=((toMin(s)-6*60)/TOTAL*100).toFixed(1);
    const width=((toMin(e)-toMin(s))/TOTAL*100).toFixed(1);
    const cls=i===todayIdx?'bar today':i===tmrIdx?'bar tomorrow-b':'bar';
    const lbl=i===tmrIdx?`<span class="lbl">${s}-${e}</span>`:'';
    track.innerHTML=`<div class="${cls}" style="left:${left}%;width:${width}%">${lbl}</div>`;
  });
}

// ===== Tomorrow strip =====
function updateTmrStrip(){
  const cfg=state.currentCfg;
  if(!cfg) return;
  const tmr=new Date(Date.now()+86400000);
  const tmrN=(tmr.getDay()+6)%7+1;
  let s='08:00', e='22:00';
  if(cfg.mode==='week_time' && cfg.time?.week_time?.[String(tmrN)]){
    [s,e]=cfg.time.week_time[String(tmrN)].split('-');
  } else if(cfg.time?.tomorrow){ [s,e]=cfg.time.tomorrow.split('-'); }
  const seat=(cfg.seat_list||[])[0]||'?';
  const ss=document.querySelector('#tmr-strip .info .ss');
  if(ss) ss.textContent=`明早 06:00 抢 ${seat} · ${s}-${e}`;
  const tsEl=document.getElementById('tmr-start');
  const teEl=document.getElementById('tmr-end');
  if(tsEl) tsEl.value=s;
  if(teEl) teEl.value=e;
}

function toggleTmr(){
  state.tmrOpen=!state.tmrOpen;
  document.getElementById('tmr-strip').classList.toggle('open',state.tmrOpen);
  document.getElementById('tmr-body').classList.toggle('open',state.tmrOpen);
}
function closeTmr(){ state.tmrOpen=false; document.getElementById('tmr-strip').classList.remove('open'); document.getElementById('tmr-body').classList.remove('open'); }
function saveTmr(){ toast('明日临时调整已记录','success'); closeTmr(); }

// ===== Toggle switch =====
function tToggle(el,e){ e&&e.stopPropagation(); el.classList.toggle('on'); }

// ===== Arrived =====
function toggleArrived(){
  state.arrived=!state.arrived;
  const btn=document.getElementById('btn-arrived');
  if(state.arrived){
    btn.textContent='✓ 已到馆'; btn.classList.remove('accent'); btn.classList.add('primary');
    toast('已标记到馆，迟到保护今日不触发','success');
  } else {
    btn.textContent='✓ 我已到馆'; btn.classList.remove('primary'); btn.classList.add('accent');
    toast('已取消到馆标记','info');
  }
}

// ===== Notices =====
async function loadNotices(){
  if(!state.authed){
    const list=document.getElementById('notice-list');
    if(list) list.innerHTML='<div class="box tight ghost" style="text-align:center"><div class="sub" style="color:var(--ink3)">登录后查看公告与预约结果</div></div>';
    return;
  }
  try {
    const [ar,rr]=await Promise.all([
      fetch('/api/announcements').catch(()=>null),
      fetch('/api/my/reservation_results').catch(()=>null)
    ]);
    const anns=ar&&ar.ok?await ar.json():[];
    const results=rr&&rr.ok?await rr.json():[];
    renderNotices(anns,results);
  } catch(e){}
}

function renderNotices(anns,results){
  const list=document.getElementById('notice-list');
  if(!list) return;
  const items=[];

  (anns||[]).forEach(a=>{
    const pin=a.pinned?'<span class="pill muted" style="font-size:10px;margin-right:2px">置顶</span>':'';
    const bc={success:'var(--ok)',warning:'var(--warn)',danger:'var(--danger)'}[a.level]||'var(--accent)';
    items.push(`<div class="box tight" style="border-left:5px solid ${bc}">
      <div class="row-flex" style="gap:6px">${pin}<span class="pill accent">公告</span><div class="sub">${esc(a.title)}</div></div>
      <div class="t mt" style="margin-top:4px">${esc(a.content)}</div>
      <div class="tiny mt">${esc(a.updated_at||a.created_at||'')}</div>
    </div>`);
  });

  (results||[]).forEach(r=>{
    if(!r.result) return;
    const ok=r.success;
    const bc=ok?'var(--ok)':'var(--danger)';
    const pillStyle=ok?'':'border-color:var(--danger);color:var(--danger);background:rgba(225,29,72,.08)';
    items.push(`<div class="box tight" style="border-left:5px solid ${bc}">
      <div class="row-flex" style="gap:6px">
        <span class="pill ${ok?'ok':''}" style="${pillStyle}">${ok?'预约成功':'预约失败'}</span>
        <div class="sub">学号 ${esc(r.pid)}</div>
      </div>
      <div class="t mt" style="margin-top:4px">${esc(r.result)}</div>
      <div class="tiny mt">${esc(r.updated_at||'')}</div>
    </div>`);
  });

  list.innerHTML=items.length
    ? items.join('')
    : '<div class="box tight ghost" style="text-align:center"><div class="sub" style="color:var(--ink3)">暂无通知</div></div>';
}

function toggleNotices(){
  state.noticesCollapsed=!state.noticesCollapsed;
  document.getElementById('notice-list').style.display=state.noticesCollapsed?'none':'';
  document.getElementById('notice-toggle').textContent=state.noticesCollapsed?'展开':'折叠';
}

// ===== Seats =====
async function loadSeats(){
  try {
    const r=await fetch('/api/seats');
    const d=await r.json();
    if(d.seats) state.allSeats=d.seats;
  } catch(e){}
}

function onZone(z){
  const sel=document.getElementById('sp-seat');
  const seats=state.allSeats[z]||[];
  if(!z||!seats.length){ sel.innerHTML='<option>先选楼层</option>'; return; }
  sel.innerHTML=seats.map(s=>`<option>${esc(s)}</option>`).join('');
}

function addSeat(){
  const z=document.getElementById('sp-zone').value;
  const s=document.getElementById('sp-seat').value;
  if(!z||!s||s==='先选楼层'){ toast('请先选择','error'); return; }
  if(state.cfgSeats.includes(s)){ toast('已添加过该座位','error'); return; }
  state.cfgSeats.push(s);
  renderCfgSeats();
  toast('已添加 '+s,'success');
  closeSheet();
}

function removeSeat(x){
  const chip=x.closest('.chip');
  chip.style.transition='opacity .2s'; chip.style.opacity='0';
  setTimeout(()=>chip.remove(),200);
}

// ===== Live reservations =====
async function queryLiveReservations(){
  if(!state.currentPid){ toast('请先选择学号','error'); return; }
  const listEl=document.getElementById('sheet-resv-list');
  if(listEl) listEl.innerHTML='<div class="sub" style="color:var(--ink3)">登录中，请稍候...</div>';
  try {
    const r=await fetch(`/api/my/accounts/${state.currentPid}/reservations`);
    const d=await r.json();
    if(!listEl) return;
    if(d.error){ listEl.innerHTML=`<div class="sub" style="color:var(--danger)">${esc(d.error)}</div>`; return; }
    if(!d.reservations?.length){ listEl.innerHTML='<div class="sub" style="color:var(--ink3)">当前没有预约</div>'; return; }
    const fmtSt=s=>({1:'待签到',2:'使用中',3:'暂离',4:'已结束',5:'已取消',1093:'已违约'}[s]||`(${s})`);
    listEl.innerHTML=d.reservations.map(resv=>{
      const seat=resv.devInfo?.devName||'未知';
      const canCancel=resv.resvStatus<=3;
      return `<div class="box tight" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div>
          <div class="mono" style="font-size:15px;font-weight:600">${esc(seat)}</div>
          <div class="tiny">${esc(resv.resvBeginTime||'')} ~ ${esc(resv.resvEndTime||'')} · ${esc(fmtSt(resv.resvStatus))}</div>
        </div>
        ${canCancel?`<button class="btn sm danger" onclick="cancelResv('${esc(resv.uuid)}')">取消</button>`:''}
      </div>`;
    }).join('');
  } catch(e){ if(listEl) listEl.innerHTML='<div class="sub" style="color:var(--danger)">查询失败</div>'; }
}

async function cancelResv(uuid){
  if(!confirm('确定取消该预约？')) return;
  try {
    const r=await fetch(`/api/my/accounts/${state.currentPid}/cancel`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uuid})
    });
    const d=await r.json();
    toast(d.message||'完成',d.success?'success':'error');
    if(d.success) queryLiveReservations();
  } catch(e){ toast('失败','error'); }
}

async function reserveNow(){
  if(!state.currentPid){ toast('请先登录并选择学号','error'); return; }
  const btn=document.querySelector('.btn[onclick*="reserveNow"]');
  if(btn){ btn.disabled=true; btn.textContent='抢座中...'; }
  try {
    const r=await fetch(`/api/my/accounts/${state.currentPid}/reserve_now`,{method:'POST'});
    const d=await r.json();
    toast(d.message||d.error, r.ok?'success':'error');
  } catch(e){ toast('网络错误','error'); }
  finally { if(btn){ btn.disabled=false; btn.textContent='⚡ 立即预约'; } }
}

// ===== Notification settings =====
async function saveEmail(){
  if(!state.currentPid){ toast('请先登录并选择学号','error'); return; }
  const val=(document.getElementById('em-input').value||'').trim();
  await saveNotifyField('notify_email',val);
  const el=document.getElementById('email-label');
  if(el) el.textContent=val||'未设置';
  toast('邮箱已保存','success'); closeSheet();
}

async function saveSC(){
  if(!state.currentPid){ toast('请先登录并选择学号','error'); return; }
  const val=(document.getElementById('sc-input').value||'').trim();
  await saveNotifyField('notify_serverchan_key',val);
  const el=document.getElementById('sc-label');
  if(el) el.textContent=val?'已配置':'未配置';
  toast('SendKey 已保存','success'); closeSheet();
}

async function saveNotifyField(field,value){
  try {
    await fetch(`/api/my/accounts/${state.currentPid}`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[field]:value})
    });
    if(state.currentCfg) state.currentCfg[field]=value;
  } catch(e){}
}

// ===== Settings page =====
function renderSettingsPage(){
  const nameEl=document.querySelector('#page-settings .h3');
  const countEl=document.querySelector('#page-settings .tiny');
  if(state.authed){
    if(nameEl) nameEl.textContent=state.uid||'';
    if(countEl) countEl.textContent=`已登录 · ${state.accounts.length} 个学号`;
  } else {
    if(nameEl) nameEl.textContent='未登录';
    if(countEl) countEl.textContent='点击右侧按钮登录或切换账号';
  }
  const cfg=state.currentCfg||{};
  const emailEl=document.getElementById('email-label');
  if(emailEl) emailEl.textContent=cfg.notify_email||'未设置';
  const scEl=document.getElementById('sc-label');
  if(scEl) scEl.textContent=cfg.notify_serverchan_key?'已配置':'未配置';
}

// ===== Sheets =====
const SHEETS = {
  auth: ()=>`
    <div class="grab"></div>
    <h3 id="auth-title">登录 AutoLib</h3>
    <div class="desc">填写你的 AutoLib 账号，管理图书馆预约。</div>
    <div class="seg mt" id="auth-seg" style="margin-bottom:12px">
      <button class="on" onclick="authSwitchMode('login',this)">登录</button>
      <button onclick="authSwitchMode('register',this)">注册</button>
    </div>
    <div class="col gap-sm">
      <div class="field"><label>用户名</label>
        <input type="text" id="auth-uid" placeholder="你的用户名" autocomplete="username"></div>
      <div class="field"><label>密码</label>
        <input type="password" id="auth-pass" placeholder="登录密码" autocomplete="current-password"
          onkeydown="if(event.key==='Enter')doAuth(_authMode)"></div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn accent grow" id="auth-submit" onclick="doAuth(_authMode)">登录</button>
    </div>
  `,
  accounts: ()=>`
    <div class="grab"></div>
    <h3>我的学号</h3>
    <div class="desc">选择要操作的学号，或添加新学号。</div>
    <div class="col gap-sm">
      ${state.accounts.length ? state.accounts.map(a=>`
        <div class="box tight clickable ${a.pid===state.currentPid?'accent':''}" onclick="selectAccount('${esc(a.pid)}')">
          <div class="row-between">
            <div>
              <div class="mono" style="font-size:15px;font-weight:600">${esc(a.pid)}</div>
              <div class="tiny" style="margin-top:2px">
                ${a.verified?'✓ 已验证':'⚠ 未验证'} ·
                ${a.is_reserved==='True'?'<span style="color:var(--ok)">运行中</span>':'<span style="color:var(--ink3)">已暂停</span>'}
                · ${(a.seat_list||[]).length} 个座位
              </div>
            </div>
            <div class="row-flex" style="gap:6px">
              ${a.pid===state.currentPid?'<span class="pill accent" style="font-size:10px">当前</span>':''}
              <button class="btn sm danger" onclick="event.stopPropagation();deleteAccount('${esc(a.pid)}')">删除</button>
            </div>
          </div>
        </div>
      `).join('') : '<div class="sub" style="color:var(--ink3)">还没有学号，添加一个吧</div>'}
      <div class="box tight ghost clickable" style="text-align:center" onclick="openSheet('add-pid')">
        <div class="sub" style="color:var(--ink3)">+ 添加学号</div>
      </div>
    </div>
  `,
  'add-pid': ()=>`
    <div class="grab"></div>
    <h3>添加新学号</h3>
    <div class="desc">填写学号和两个密码，我们会先验证再保存。</div>
    <div class="col gap-sm">
      <div class="field"><label>学号</label>
        <input type="text" id="add-pid-input" placeholder="18210xxxxx"></div>
      <div class="field"><label>VPN 密码（统一身份认证）</label>
        <input type="password" id="add-vpn" placeholder="webvpn 密码"></div>
      <div class="field"><label>图书馆密码（IC空间）</label>
        <input type="password" id="add-lib" placeholder="默认 njfu学号!"></div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="openSheet('accounts')">取消</button>
      <button class="btn accent grow" id="add-submit" onclick="verifyAndAdd()">验证并保存</button>
    </div>
  `,
  cancel: ()=>`
    <div class="grab"></div>
    <h3>当前预约</h3>
    <div class="desc">查询实时预约，可点击「取消」释放座位。</div>
    <div class="col gap-sm" id="sheet-resv-list">
      <div class="sub" style="color:var(--ink3)">加载中...</div>
    </div>
    <div class="row-flex mt">
      <button class="btn ghost grow" onclick="closeSheet()">关闭</button>
      <button class="btn sm ghost" onclick="queryLiveReservations()">刷新</button>
    </div>
  `,
  'seat-picker': ()=>`
    <div class="grab"></div>
    <h3>添加座位</h3>
    <div class="desc">先选楼层，再选座位号。加入后出现在优先级列表末尾。</div>
    <div class="col gap-sm">
      <div class="field"><label>楼层 / 区域</label>
        <select id="sp-zone" onchange="onZone(this.value)">
          <option value="">选择楼层...</option>
          ${Object.keys(state.allSeats).sort((a,b)=>{
            const fa=Number((a.match(/\d+/)||[99])[0]);
            const fb=Number((b.match(/\d+/)||[99])[0]);
            return fa-fb||a.localeCompare(b);
          }).map(k=>`<option value="${esc(k)}">${esc(k)} (${state.allSeats[k].length}个)</option>`).join('')}
        </select>
      </div>
      <div class="field"><label>座位号</label>
        <select id="sp-seat"><option>先选楼层</option></select>
      </div>
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="addSeat()">添加</button>
    </div>
  `,
  email: ()=>`
    <div class="grab"></div>
    <h3>邮箱通知</h3>
    <div class="desc">预约结果通过邮件推送。留空即不发送。</div>
    <div class="field"><label>邮箱地址</label>
      <input type="email" id="em-input" value="${esc(state.currentCfg?.notify_email||'')}" placeholder="xxx@qq.com">
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="saveEmail()">保存</button>
    </div>
  `,
  sc: ()=>`
    <div class="grab"></div>
    <h3>Server酱 SendKey</h3>
    <div class="desc">在 sct.ftqq.com 免费获取，用于微信推送预约结果。</div>
    <div class="field"><label>SendKey</label>
      <input type="text" id="sc-input" value="${esc(state.currentCfg?.notify_serverchan_key||'')}" placeholder="SCTxxxxxx">
    </div>
    <div class="row-flex mt-lg">
      <button class="btn ghost grow" onclick="closeSheet()">取消</button>
      <button class="btn accent grow" onclick="saveSC()">保存</button>
    </div>
  `,
  'lp-info': ()=>`
    <div class="grab"></div>
    <h3>🛡 关于迟到保护</h3>
    <div class="desc">开启后，系统会在预约开始前检查是否已到馆。</div>
    <div class="col gap-sm">
      <div class="box tight" style="border-left:4px solid var(--accent)">
        <div class="sub" style="font-weight:700">✓ 最多保护 1 小时</div>
        <div class="t">未按时到馆则自动把预约推迟 1 小时</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--ok)">
        <div class="sub" style="font-weight:700">✓ 到馆后手动确认</div>
        <div class="t">请点击主页的「我已到馆」按钮，避免误触发</div>
      </div>
      <div class="box tight" style="border-left:4px solid var(--danger)">
        <div class="sub" style="font-weight:700">⚠ 1小时后仍未到</div>
        <div class="t">系统将自动释放预约，杜绝恶意占座</div>
      </div>
    </div>
    <button class="btn primary mt-lg" style="width:100%" onclick="closeSheet()">我知道了</button>
  `,
};

function openSheet(name){
  const sc=document.getElementById('scrim');
  const content=document.getElementById('sheet-content');
  content.innerHTML=SHEETS[name]?SHEETS[name]():'<div class="grab"></div><h3>功能未实现</h3>';
  sc.classList.add('show');
  sc.classList.toggle('center',['lp-info'].includes(name));
  if(name==='cancel') setTimeout(queryLiveReservations,80);
}

function closeSheet(){
  document.getElementById('scrim').classList.remove('show');
}

document.getElementById('scrim').addEventListener('click', e=>{
  if(e.target.id==='scrim') closeSheet();
});

// ===== Tweaks =====
function applyTweaks(){
  const t=state.tweaks;
  document.documentElement.style.setProperty('--accent',t.accent);
  const hex=t.accent.replace('#','');
  const r=parseInt(hex.slice(0,2),16),g=parseInt(hex.slice(2,4),16),b=parseInt(hex.slice(4,6),16);
  document.documentElement.style.setProperty('--accent-soft',`rgba(${r},${g},${b},.08)`);
  document.documentElement.style.setProperty('--accent-softer',`rgba(${r},${g},${b},.04)`);
  document.body.classList.toggle('compact',t.density==='compact');
  document.body.classList.toggle('cozy',t.density==='cozy');
  document.body.classList.toggle('no-wobble',!t.wobble);
  document.documentElement.style.setProperty('--hand',`'${t.font}',cursive`);
}

function bindTweaks(){
  const setVal=(id,key)=>{
    const el=document.getElementById(id);
    if(!el) return;
    const isCb=el.type==='checkbox';
    if(isCb) el.checked=state.tweaks[key]; else el.value=state.tweaks[key];
    el.addEventListener('change',()=>{
      state.tweaks[key]=isCb?el.checked:el.value;
      applyTweaks();
      try{window.parent.postMessage({type:'__edit_mode_set_keys',edits:state.tweaks},'*')}catch(e){}
    });
  };
  setVal('tw-accent','accent');
  setVal('tw-density','density');
  setVal('tw-wobble','wobble');
  setVal('tw-font','font');
}

window.addEventListener('message', e=>{
  const d=e.data;
  if(!d?.type) return;
  if(d.type==='__activate_edit_mode') document.getElementById('tweaks').classList.add('show');
  if(d.type==='__deactivate_edit_mode') document.getElementById('tweaks').classList.remove('show');
});

// ===== Date =====
function updateDate(){
  const ENG=['SUN','MON','TUE','WED','THU','FRI','SAT'];
  const MONS=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  const CN=['日','一','二','三','四','五','六'];
  const d=new Date();
  const da=d.getDate(), mo=d.getMonth()+1;
  document.getElementById('today-date-label').textContent=`周${CN[d.getDay()]} · ${mo}月${da}日`;
  const dateEl=document.getElementById('date');
  if(dateEl) dateEl.textContent=`${ENG[d.getDay()]} · ${MONS[d.getMonth()]} ${String(da).padStart(2,'0')} · ${d.getFullYear()}`;
  const tmr=new Date(d.getTime()+86400000);
  const tnEl=document.getElementById('tmr-n');
  if(tnEl) tnEl.textContent=tmr.getDate();
  const twEl=document.querySelector('#tmr-strip .day-box .wd');
  if(twEl) twEl.textContent=ENG[tmr.getDay()];
}

// ===== Init =====
buildWeekGrid();
updateDate();
bindTweaks();
applyTweaks();
renderHomeCard();
renderWeekBars();
loadNotices();
loadSeats();   // 公开接口，游客也需要用座位选择器
checkAuth();

try{window.parent.postMessage({type:'__edit_mode_available'},'*')}catch(e){}
