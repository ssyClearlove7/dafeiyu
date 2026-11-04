const PRODUCT_CONSOLE = document.querySelector('meta[name=console-runtime]')?.content === 'product';
let CONSOLE_CSRF = null;

const $ = (s) => document.querySelector(s);
let TAB = "overview", OVERVIEW = null, TUNING = null, ROOMS = [], WHITELIST = [],
    ADMINS = [];
let BOT_BUSY = false;

function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.style.background = bad ? "#5a2626" : "#1d3f61";
  t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 2200);
}
async function api(path, body) {
  if (body !== undefined && PRODUCT_CONSOLE && !CONSOLE_CSRF) {
    const response = await fetch('/api/console/state');
    const result = await response.json();
    if (!response.ok || !result.csrf) throw Error('控制台校验失败，请刷新');
    CONSOLE_CSRF = result.csrf;
  }
  const opt = body !== undefined ? {method: "POST", headers: {"Content-Type": "application/json", ...(PRODUCT_CONSOLE ? {"X-CSRF-Token":CONSOLE_CSRF} : {})}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ok: false, error: "返回不是 JSON"}));
  if (!r.ok || j.ok === false) toast(j.error || "失败", true);
  return j;
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }
function roomOptions(sel) {
  return ROOMS.map((r) => `<option value="${esc(r.room)}"${r.room === sel ? " selected" : ""}>${esc(r.name || r.room)}</option>`).join("");
}

async function loadOverview() {
  const [j,usage] = await Promise.all([api("/api/overview"), TAB === "overview" ? api("/api/usage?days="+USAGE_DAYS) : Promise.resolve(null)]);
  if (usage?.ok && usage.days.length===USAGE_DAYS) USAGE_DATA=usage;
  if (!j.ok) return;
  OVERVIEW = j;
  const b = $("#badgeBot"), h = $("#badgeHook");
  b.className = "badge " + (j.bot.running ? "ok" : "bad");
  b.textContent = j.bot.running ? (j.product?'机器人：运行中':j.bot.round == null ? `机器人：等待配置 (#${j.bot.pid})` : `机器人：运行中 (#${j.bot.pid})`) : "机器人：没在跑";
  h.className = "badge " + (j.hook.ok ? "ok" : "bad");
  h.textContent = "微信：" + (j.hook.ok ? "正常" : "不通");
  $("#badgeTime").textContent = j.now;
  $("#badgeTime").className = "badge ok";
  if ((TAB === "overview" || TAB === "bot") && !BOT_BUSY) render();
}

async function setBotRunning(action) {
  if (BOT_BUSY) return;
  BOT_BUSY = true;
  const controls = ["#botStart", "#botStop"];
  controls.forEach(s => { if ($(s)) $(s).disabled = true; });
  try {
    const r = await api("/api/bot", {action});
    if (r.ok) {
      const expected = action === "start";
      const matched = !!r.bot?.running === expected;
      toast(matched ? (expected ? "机器人已启动" : "机器人已停止") : "操作后状态未确认，请查看日志", !matched);
    }
    await loadOverview();
  } catch (e) {
    toast("操作失败：" + e.message, true);
  } finally {
    BOT_BUSY = false;
    if (TAB === "bot") renderBot();
    else controls.forEach(s => { if ($(s)) $(s).disabled = false; });
  }
}

function render() {
  if (TAB === "overview") return renderOverview();
  if (TAB === "bot") return renderBot();
  if (TAB === "model") return renderModel();
  if (TAB === "wechat") return renderWechat();
  if (TAB === "voice") return renderVoice();
  if (TAB === "memory") return renderMemory();
  if (TAB === "tuning") return renderTuning();
  if (TAB === "lists") return renderLists();
  if (TAB === "persona") return renderPersona();
  if (TAB === "stickers") return renderStickers();
  if (TAB === "tools") return renderTools();
  if (TAB === "log") return renderLog();
  if (TAB === "maintenance" && PRODUCT_CONSOLE) return renderMaintenance();
}

function renderOverview() {
  const o = OVERVIEW || {},bot=o.bot||{},connected=!!o.hook?.ok;
  const running=bot.running?(o.product?'运行中':bot.round==null?(connected?'等待配置会话':'等待微信就绪'):'运行中'):'已停止';
  const mutes=(o.mutes||[]).map(m=>`<span class="tag">${esc(o.rooms?.[m.scope]||m.scope)} · 剩余 ${m.left} 秒</span>`).join('');
  const curfew=(o.curfew?.periods||[]).map(period=>`<span class="tag">${esc(period.start)} — ${esc(period.end)}</span>`).join('');
  $('#view').innerHTML=`<div class="overview-layout">
    <div class="card overview-status">
      <div class="overview-section-head"><h2>服务状态</h2><span class="overview-refresh"><i></i>每 5 秒更新</span></div>
      <div class="overview-services">
        <div class="overview-service"><span class="overview-service-icon" aria-hidden="true">◉</span><div><p class="overview-label">自动回复服务</p><p class="overview-service-value"><span class="status-light ${bot.running?'is-on':''}"></span>${running}</p></div></div>
        <div class="overview-service"><span class="overview-service-icon" aria-hidden="true">◇</span><div><p class="overview-label">微信连接</p><p class="overview-service-value"><span class="status-light ${connected?'is-on':''}"></span>${connected?'已连接':'未连接'}</p></div><span class="overview-account" title="${esc(o.hook?.wxid||'未登录')}">${esc(o.hook?.wxid||'未登录')}</span></div>
      </div>
      <dl class="overview-metadata"><div><dt>进程 PID</dt><dd>${bot.pid??'—'}</dd></div><div><dt>最近心跳</dt><dd>${bot.age!=null?bot.age+' 秒前':'—'}</dd></div><div><dt>运行轮次</dt><dd>${bot.round??'—'}</dd></div><div><dt>会话数量</dt><dd>${Object.keys(o.rooms||{}).length}<small>个</small></dd></div><div><dt>接入方式</dt><dd>${esc(o.backend_label||(o.backend==='native'?'Ubuntu 原生':'Windows Hook'))}</dd></div></dl>
    </div>
    <div class="overview-policies">
      <div class="card overview-policy"><div class="overview-section-head"><h2>静音</h2><span class="overview-policy-state ${(o.mutes||[]).length?'is-resting':''}">${(o.mutes||[]).length?'静音中':'未静音'}</span></div><div class="overview-policy-content">${mutes||'<span class="hint">当前没有静音会话</span>'}</div></div>
      <div class="card overview-policy"><div class="overview-section-head"><h2>宵禁</h2><span class="overview-policy-state ${o.curfew?.active?'is-resting':''}">${o.curfew?.active?'休息中':'可聊天'}</span></div><div class="overview-policy-content">${curfew||'<span class="hint">未设置，全天可聊</span>'}</div></div>
    </div>
    ${usageMarkup()}
  </div>`;
  decorateView();
  const note=$('.overview-chart-note'),chart=$('.overview-layout .usage-chart');
  if(note)note.ontoggle=()=>{OVERVIEW_NOTE_OPEN=note.open;};
  if(chart){chart.scrollLeft=OVERVIEW_CHART_SCROLL;chart.onscroll=()=>{OVERVIEW_CHART_SCROLL=chart.scrollLeft;};}
}

async function renderModel(){
  $("#view").innerHTML = `    <div class="card" id="keyCard"><h2 style="margin-bottom:6px">聊天与联网 · DeepSeek</h2>
      <div class="hint" id="keyState">读取中…</div>
      <div class="row" style="margin-top:8px"><div><label>API Key（聊天与联网共用）</label>
        <input type="password" autocomplete="off" id="keyInput" placeholder="sk-..."></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="keySave">保存</button>
      <button class="ghost" id="keyTest">检查聊天连接</button></div>
<div class="row" style="margin-top:10px"><button class="act" id="keyCap">重新抓数据库 key</button></div>
<p class="hint" id="keyCapMsg">换过微信号、重新装过微信、或 keys.json 被清空时点这个。点了会弹 UAC，然后切到微信点开任意聊天。</p>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:8px">
        <input type="checkbox" id="keyAllowDsh"> 允许读取本机 DeepSeek Harness 凭证（默认关；关掉后只认这里填的 Key 或环境变量）</label>
      <p class="hint" id="keyMsg"></p></div>`;
  $('#view').insertAdjacentHTML('beforeend', `<div class="card"><h2>联网搜索</h2><p class="hint">聊天和联网共用上方的 DeepSeek Key，无需单独配置模型或密钥。</p><label class="check-label"><input type="checkbox" id="webEnabled">允许根据问题搜索网页</label><p id="webState" class="hint">正在读取…</p><div class="row"><button class="act" id="webSave">保存联网设置</button><button class="ghost" id="webTest">检查联网（会产生一次调用）</button></div></div>`);
  decorateView();
  await Promise.all([bindKey(),bindWeb()]);
  if(PRODUCT_CONSOLE) await renderProductModels();
}

async function bindKey() {
  if (!$("#keyCard")) return;
  if (OVERVIEW?.backend === "native") {
    $("#keyCap").style.display = "none";
    $("#keyCapMsg").hidden = true;
  }
  const ks = await api("/api/apikey");
  if (!ks.ok || !$("#keyState")) return;
  $("#keyState").innerHTML = ks.set
      ? `当前：<b>${esc(ks.masked)}</b>　来源：${esc(ks.source)}`
      : "当前：<b>没有 Key</b>　（聊天会直接报错，先填一个）";
  const cb = $("#keyAllowDsh");
  if(PRODUCT_CONSOLE && cb) cb.closest("label").remove();
  if (cb) {
    cb.checked = !!ks.allowDsh;
    cb.onchange = async () => {
      const r = await api("/api/apikey", {allowDsh: cb.checked});
      if (r.ok) { toast(cb.checked ? "已允许读取本机 DSH 凭证" : "已关闭：只认这里填的 Key / 环境变量"); bindKey(); }
    };
  }
  $("#keySave").onclick = async () => {
    const r = await api("/api/apikey", {key: $("#keyInput").value});
    if (r.ok) { toast("已保存到 settings.json，下一轮就能用"); bindKey(); }
  };
  $("#keyTest").onclick = async () => {
    $("#keyMsg").textContent = "测试中…";
    const r = await api("/api/apikey/test", {key: $("#keyInput").value});
    if (!$("#keyMsg")) return;
    $("#keyMsg").textContent = r.ok ? "✅ 能用：" + (r.detail || "").slice(0, 120) : "❌ " + (r.error || "失败");
  };
}

async function renderTuning() {
  const j = await api("/api/tuning");
  if (TAB !== "tuning") return;
  if (!j.ok) return;
  TUNING = j;
  const groups = j.groups.map((g) => `
    <div class="card"><h2 style="margin-bottom:6px">${esc(g.title)}</h2>
      ${g.keys.includes("IDLE_TOPIC_P") ? `<p class="hint">主动抛话题和饭点报菜单仅在「活跃参与」模式下对已启用的群聊生效；「情绪参与」「与我有关」「点名参与」模式均不触发，私聊不触发。宵禁或群聊被禁言时也不发送。</p>
      <p class="hint">群里连续无消息达到 IDLE_TOPIC_AFTER 后，才按 IDLE_TOPIC_P 的概率尝试抛话题（0.9 表示每次尝试有 90% 的概率，并非发言频率）。未抽中时，间隔 IDLE_TOPIC_EVERY 秒再尝试；群里出现新消息（含她自己的发言）会重新计算静默时间。</p>` : ""}
      ${g.keys.map((k) => {
        const s = j.spec[k], v = j.values[k];
        if (s.options) return `<label>群聊参与方式</label>
          <select data-k="${k}">${s.options.map((name) => `<option value="${esc(name)}" ${v === name ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>
          <p class="hint">活跃参与：保持当前接话程度；情绪参与：主动回应明确的喜怒哀乐，普通陈述不接；与我有关：谈论或回应我时加入；点名参与：被 @ 或叫到别名才加入。被叫到都会发言，加入后无需反复点名，完成或换题后退出。除活跃参与外，不主动抛话题或报菜单。</p>`;
        return `<label title="${esc(s.desc)}">${k} <span class="hint">— ${esc(s.desc)}</span></label>
          <input type="number" step="${s.type === "int" ? "1" : "0.01"}" min="${s.min}" max="${s.max}"
                 data-k="${k}" value="${v}">`;
      }).join("")}
    </div>`).join("");
  $("#view").innerHTML = `<h2>参数 <span class="hint">保存后主循环下一轮生效，不用重启</span></h2>
    <div class="card"><h2>宵禁时间段</h2>
      <p class="hint">每天按北京时间执行。使用24小时制 HH:MM，例如 00:30–07:30；结束早于开始表示跨午夜。${PRODUCT_CONSOLE?'免打扰期间不自主新开话题，有人找时仍可回应；明确设置的提醒照常发送。当前支持一个时段。':'开始时刻进入宵禁，结束时刻恢复；未设置时全天可聊。'}</p>
      <div id="curfewPeriods"></div>
      <button class="ghost" id="addCurfew" style="margin-top:10px">添加时段</button>
    </div>${groups}
    <div class="row"><button class="act" id="saveTuning">保存参数</button>
    <button class="ghost" id="resetTuning">恢复默认</button></div>
    <p class="hint">文件：${esc(j.file)}</p>`;
  function addPeriod(period = {start: "", end: ""}) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.curfew = "1";
    row.style.marginTop = "8px";
    row.innerHTML = `<div><label>开始时间（24小时制）</label><input type="text" data-time="start" inputmode="numeric" placeholder="23:00" maxlength="5" value="${esc(period.start)}"></div>
      <div><label>结束时间（24小时制）</label><input type="text" data-time="end" inputmode="numeric" placeholder="07:00" maxlength="5" value="${esc(period.end)}"></div>
      <button class="ghost danger" type="button">删除时段</button>`;
    row.querySelector("button").onclick = () => row.remove();
    $("#curfewPeriods").appendChild(row);
  }
  (j.values.CURFEW_PERIODS || []).forEach(addPeriod);
  $("#addCurfew").onclick = () => addPeriod();
  if(j.singlePeriod){
    $('#addCurfew').onclick=()=>{if(document.querySelector('[data-curfew]'))return toast('当前安装版支持一个每日免打扰时段',true);addPeriod();};
  }
  $("#saveTuning").onclick = async () => {
    const patch = {};
    patch.CURFEW_PERIODS = [...document.querySelectorAll("[data-curfew]")].map((row) => ({
      start: row.querySelector('[data-time="start"]').value,
      end: row.querySelector('[data-time="end"]').value,
    }));
    if (patch.CURFEW_PERIODS.some((p) => !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(p.start) || !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(p.end) || p.start === p.end)) {
      toast("每段须填写有效的24小时制时间 HH:MM，开始和结束不能相同", true);
      return;
    }
    document.querySelectorAll("[data-k]").forEach((el) => { patch[el.dataset.k] = el.value; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已保存：" + r.changed.join("、")); renderTuning(); }
  };
  $("#resetTuning").onclick = async () => {
    const patch = {};
    Object.keys(j.spec).forEach((k) => { patch[k] = j.spec[k].default; });
    const r = await api("/api/tuning", {patch});
    if (r.ok) { toast("已恢复默认"); renderTuning(); }
  };
}

async function renderLists(refresh = true) {
  if (refresh) {
    const [rj, wj, aj] = await Promise.all([api("/api/rooms"), api("/api/whitelist"), api("/api/admins")]);
    ROOMS = rj.rooms || []; WHITELIST = wj.whitelist || []; ADMINS = aj.admins || [];
  }
  const roomRows = ROOMS.map((r, i) => `<tr>
      <td><input type="text" data-r="${i}" data-f="name" value="${esc(r.name || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="room" value="${esc(r.room || "")}"></td>
      <td><input type="text" data-r="${i}" data-f="account" value="${esc(r.account || "")}"></td>
      <td style="text-align:center"><input type="checkbox" data-r="${i}" data-f="enabled" ${r.enabled ? "checked" : ""}></td>
      <td><button class="ghost danger" data-del="${i}">删</button></td></tr>`).join("");
  const adminRows = ADMINS.map((r, i) => `<tr>
      <td><input type="text" aria-label="管理员备注名称" data-a="${i}" data-f="name" value="${esc(r.name || "")}"></td>
      <td><input type="text" aria-label="管理员微信 ID 或名字" data-a="${i}" data-f="wxid" value="${esc(r.wxid || "")}"></td>
      <td><button class="ghost danger" data-admin-del="${i}">删</button></td></tr>`).join("");
  $("#view").innerHTML = `
    <h2>白名单</h2>
    <p class="hint">白名单已经和会话合并：这张表就是白名单，<b>勾了「启用」才会听、才会发</b>；取消勾选等于移除。</p>
    <div class="card"><h2 style="margin-bottom:6px">会话 / 白名单</h2>
      <table style="width:100%;border-collapse:collapse"><thead><tr>
        <th align="left">名字<small class="list-column-hint">会话备注；room 留空时用此名称识别</small></th>
        <th align="left">会话 ID / 名字（room）<small class="list-column-hint">群 ID 或好友 wxid，也可填微信名称；保存时转为 ID</small></th>
        <th align="left">机器人账号（account）<small class="list-column-hint">接收和发送消息的机器人 wxid；留空自动使用当前账号</small></th>
        <th>启用<small class="list-column-hint">勾选后允许该会话收发消息</small></th><th></th>
      </tr></thead><tbody>${roomRows}</tbody></table>
      <div class="row" style="margin-top:10px"><button class="ghost" id="addRoom">+ 加一行</button>
      <button class="act" id="saveRooms">保存会话</button></div></div>
    <div class="card"><h2 style="margin-bottom:6px">管理员</h2>
      <p class="hint">每行一位管理员，只有名单中的人能使用 /闭嘴 等管理员指令；保存后下一轮生效。</p>
      <table style="width:100%;border-collapse:collapse"><thead><tr>
        <th align="left">备注名称<small class="list-column-hint">方便辨认；微信 ID 留空时用此名称识别</small></th>
        <th align="left">微信 ID / 名字<small class="list-column-hint">填写管理员的 wxid 或微信名称；保存时转为 ID</small></th><th></th>
      </tr></thead><tbody>${adminRows}</tbody></table>
      <div class="row" style="margin-top:10px"><button class="ghost" id="addAdmin">+ 加一行</button>
      <button class="act" id="saveAdmins">保存管理员</button></div></div>`;
  // 输入即保留到本地草稿，加行、删除或保存另一张表时不会丢失编辑。
  document.querySelectorAll('[data-r], [data-a]').forEach(el => el.oninput = () => {
    const rows = el.hasAttribute('data-r') ? ROOMS : ADMINS;
    const i = +(el.dataset.r ?? el.dataset.a);
    rows[i][el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
  });
  // 只重绘本地数据，不要重新拉服务端（否则刚加的空行会被覆盖掉）
  $("#addRoom").onclick = () => { ROOMS.push({name: "", room: "", account: "", enabled: true}); renderLists(false); };
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => { ROOMS.splice(+b.dataset.del, 1); renderLists(false); });
  $('#addAdmin').onclick = () => { ADMINS.push({name: '', wxid: ''}); renderLists(false); };
  document.querySelectorAll('[data-admin-del]').forEach(b => b.onclick = () => { ADMINS.splice(+b.dataset.adminDel, 1); renderLists(false); });
  $("#saveRooms").onclick = async () => {
    document.querySelectorAll("[data-r]").forEach((el) => {
      const i = +el.dataset.r, f = el.dataset.f;
      ROOMS[i][f] = f === "enabled" ? el.checked : el.value.trim();
    });
    ROOMS = ROOMS.filter((r) => r.room || r.name);   // 只填了名字也别丢（m09094）
    const r = await api("/api/rooms", {rooms: ROOMS});
    if (r.ok) { ROOMS = r.rooms; toast("会话已保存"); renderLists(false); }
  };
  $("#saveAdmins").onclick = async () => {
    const r = await api("/api/admins/save", {admins: ADMINS});
    if (r.ok) { ADMINS = r.admins; toast("管理员已保存（下一轮生效）"); renderLists(false); }
  };
}

let PERSONA_PANE = 'base';
async function renderPersona() {
  const [persona, result] = await Promise.all([api('/api/persona'),api('/api/taught-rules')]);
  if(TAB!=='persona'||!persona.ok||!result.ok)return;
  let teaching=result;
  $('#view').innerHTML=`<div class="persona-tabs" role="tablist" aria-label="人设类型">${[['base','基础人设'],['evolved','演化人设'],['taught','教导人设']].map(([kind,title])=>`<button type="button" role="tab" id="persona-tab-${kind}" aria-controls="persona-pane-${kind}" data-persona-pane="${kind}">${title}</button>`).join('')}</div>
    ${[['base','基础人设','固定身份、形象和基本规则，机器人不会自行修改。'],['evolved','演化人设','记录逐渐形成的性格、偏好与表达习惯，也可以在这里人工调整。'],['taught','教导人设','通过完整文字描述性格、语气、表达和做事习惯，可以在此直接编辑。']].map(([kind,title,hint])=>`<div class="card persona-editor" id="persona-pane-${kind}" role="tabpanel" aria-labelledby="persona-tab-${kind}" data-persona-editor="${kind}"><h2>${title}</h2><p class="hint">${hint}</p><textarea id="persona-${kind}" rows="16">${esc(kind==='taught'?teaching.text:persona[kind]||'')}</textarea>${kind==='base'?'<p class="hint">点名别名写在“# 别名”段，用顿号、逗号或换行分隔。</p>':kind==='taught'?'<p class="hint">回复条数、频率等运行参数在“互动参数”中调整。</p>':''}<div class="row" style="margin-top:16px"><button class="act" data-save-persona="${kind}">保存${title}</button></div></div>`).join('')}`;
  const selectPane=kind=>{
    PERSONA_PANE=kind;
    document.querySelectorAll('[data-persona-pane]').forEach(button=>{const active=button.dataset.personaPane===kind;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
    document.querySelectorAll('[data-persona-editor]').forEach(panel=>panel.hidden=panel.dataset.personaEditor!==kind);
  };
  const tabButtons=[...document.querySelectorAll('[data-persona-pane]')];
  tabButtons.forEach((button,index)=>{
    button.onclick=()=>selectPane(button.dataset.personaPane);
    button.onkeydown=event=>{let next;if(event.key==='ArrowRight')next=(index+1)%tabButtons.length;else if(event.key==='ArrowLeft')next=(index+tabButtons.length-1)%tabButtons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabButtons.length-1;else return;event.preventDefault();tabButtons[next].focus();selectPane(tabButtons[next].dataset.personaPane);};
  });
  selectPane(PERSONA_PANE);
  document.querySelectorAll('[data-save-persona]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try{
      const kind=button.dataset.savePersona,text=$('#persona-'+kind).value;
      const saved=await api(kind==='taught'?'/api/taught-rules/save':'/api/persona/save',kind==='taught'?{text,revision:teaching.revision}:{kind,text});
      if(saved.ok){if(kind==='taught')teaching=saved;toast('已保存，下一轮自动生效');}
    }catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  });
}

async function renderStickers() {
  const [j, rj] = await Promise.all([api("/api/stickers"), api("/api/rooms")]);
  if (!j.ok || !rj.ok || TAB !== "stickers") return;
  ROOMS = rj.rooms || [];
  const items = j.items || [];
  $("#view").innerHTML = `<h2>表情包（${items.length} 张）</h2>
    <div class="row" style="margin-bottom:12px"><div><label>发到哪个会话</label>
      <select id="stRoom">${roomOptions()}</select></div></div>
    <div class="stickers">${items.map((it) => `
      <div class="sticker"><img src="/sticker/${encodeURIComponent(it.file)}" loading="lazy">
        <div class="cap">${esc(it.what || "")}<br><span class="hint">${esc(it.scene || "")}</span></div>
        <div class="ops"><button class="ghost" data-send="${esc(it.file)}">发这张</button>
        <button class="ghost danger" data-del="${esc(it.file)}">删</button></div></div>`).join("") || "<p class='hint'>库是空的</p>"}
    </div>`;
  document.querySelectorAll("[data-send]").forEach((b) => b.onclick = async () => {
    const r = await api("/api/stickers/send", {file: b.dataset.send, room: $("#stRoom").value});
    if (r.ok) toast("发了：" + b.dataset.send);
  });
  document.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("删掉 " + b.dataset.del + " ？")) return;
    const r = await api("/api/stickers/delete", {file: b.dataset.del});
    if (r.ok) { toast("已删"); renderStickers(); }
  });
}

async function renderTools() {
  const j = await api("/api/overview");
  if (TAB !== "tools") return;
  ROOMS = Object.entries(j.rooms || {}).map(([room, name]) => ({room, name}));
  $("#view").innerHTML = `<h2>消息与静音</h2>
    <div class="card"><h2 style="margin-bottom:6px">发一条消息</h2>
      <div class="row"><div><label>会话</label><select id="tRoom">${roomOptions()}</select></div></div>
      <label>内容</label><input type="text" id="tText" placeholder="测一下通不通">
      <div class="row" style="margin-top:10px"><button class="act" id="tSend">发送</button></div>
      <p class="hint">通过当前微信连接向所选会话发送消息。</p></div>
    <div class="card"><h2 style="margin-bottom:6px">静音</h2>
      <div class="row"><div><label>会话</label><select id="mRoom">${roomOptions()}</select></div>
      <div style="max-width:140px"><label>分钟</label><input type="number" id="mMin" value="10" min="1" max="1440"></div></div>
      <div class="row" style="margin-top:10px"><button class="act" id="mSet">设静音</button>
      <button class="ghost" id="mClear">全部解除</button></div>
      <p class="hint">群聊静音只停该群；私聊（管理员）静音是全体停、留管理员私聊。</p></div>
`;
  $("#tSend").onclick = async () => {
    const r = await api("/api/send", {room: $("#tRoom").value, text: $("#tText").value});
    if (r.ok) toast("已发送");
  };
  $("#mSet").onclick = async () => {
    const r = await api("/api/mute", {room: $("#mRoom").value, minutes: +$("#mMin").value});
    if (r.ok) toast("已设静音");
  };
  $("#mClear").onclick = async () => {
    const r = await api("/api/mute", {minutes: 0});
    if (r.ok) toast("已全部解除");
  };

}

async function renderLog() {
  const j = await api("/api/log?n=400");
  if (TAB !== "log") return;
  const lines = (j.lines || []).join("\n");
  $("#view").innerHTML = `<h2>日志（最后 400 行）</h2>
    <div class="card"><pre id="logBox">${esc(lines || "（还没有日志）")}</pre>
    <div class="row"><button class="ghost" id="reLog">刷新</button></div></div>`;
  const box = $("#logBox"); box.scrollTop = box.scrollHeight;
  $("#reLog").onclick = renderLog;
}

const USAGE_DAYS = 14;
let USAGE_DATA = null;
let OVERVIEW_NOTE_OPEN = false, OVERVIEW_CHART_SCROLL = 0;
const CHART_COLORS = ['#7ac7b3','#8ba9f0','#d8b77d','#c89de0','#e3919b','#7dc8df','#b9c67e','#a4adbb'];
function usageMarkup(){
  const data=USAGE_DATA;
  if(!data)return '<div class="card"><h2>每日 API 调用</h2><p class="hint">统计暂时不可用，请稍后刷新。</p></div>';
  const grouped=new Map();
  for(const row of data.rows){
    const id=JSON.stringify([row.provider,row.key_id]);
    const label=row.provider+' · '+row.key_label;
    if(!grouped.has(id))grouped.set(id,{id,label,values:{},total:0});
    const item=grouped.get(id);item.values[row.day]=(item.values[row.day]||0)+row.calls;item.total+=row.calls;
  }
  const series=[...grouped.values()].sort((a,b)=>b.total-a.total),daily=data.days.map(day=>series.reduce((sum,item)=>sum+(item.values[day]||0),0));
  const max=Math.max(...daily,1),step=Math.max(1,Math.ceil(max/4)),ceiling=step*4;
  const viewStyle=getComputedStyle($('#view'));
  const width=Math.max(700,Math.round($('#view').clientWidth-parseFloat(viewStyle.paddingLeft)-parseFloat(viewStyle.paddingRight)-48)),height=238,left=40,top=15,plotHeight=175,plotWidth=width-left-18,stride=plotWidth/data.days.length,barWidth=Math.min(26,stride*.5);
  let svg=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="北京时间每日 API 调用次数柱状图">`;
  for(let tick=0;tick<=4;tick++){const value=step*tick,y=top+plotHeight-plotHeight*value/ceiling;svg+=`<line x1="${left}" x2="${width-18}" y1="${y}" y2="${y}" class="chart-grid"/><text x="${left-9}" y="${y+4}" text-anchor="end">${value}</text>`;}
  data.days.forEach((day,index)=>{
    const x=left+stride*index+(stride-barWidth)/2;let used=0;
    const description=day+'：'+daily[index]+' 次'+series.filter(item=>item.values[day]).map(item=>'；'+item.label+' '+item.values[day]+' 次').join('');
    svg+=`<g tabindex="0" aria-label="${esc(description)}"><title>${esc(description)}</title><rect x="${left+index*stride}" y="${top}" width="${stride}" height="${plotHeight}" fill="transparent"/>`;
    series.forEach((item,seriesIndex)=>{const count=item.values[day]||0;if(!count)return;const barHeight=count/ceiling*plotHeight;svg+=`<rect x="${x}" y="${top+plotHeight-used-barHeight}" width="${barWidth}" height="${barHeight}" fill="${CHART_COLORS[seriesIndex%CHART_COLORS.length]}"/>`;used+=barHeight;});
    svg+='</g>';
    if(index%Math.max(1,Math.ceil(data.days.length/14))===0||index===data.days.length-1)svg+=`<text x="${x+barWidth/2}" y="${top+plotHeight+25}" text-anchor="middle">${day.slice(5)}</text>`;
  });
  svg+='</svg>';
  const start=new Date(data.started_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  return `<div class="card usage-card"><div class="overview-section-head"><div><h2>每日 API 调用</h2><p class="overview-chart-subtitle">${PRODUCT_CONSOLE?'查看各模型和能力的每日调用分布':'查看各密钥的每日调用分布'}</p></div><span class="overview-chart-range">近 14 天 · ${PRODUCT_CONSOLE?'按模型 / 能力':'按 API Key'}</span></div><div class="usage-legend">${series.map((item,index)=>`<span><i style="background:${CHART_COLORS[index%CHART_COLORS.length]}"></i>${esc(item.label)}<b>${item.total}</b></span>`).join('')||'<span class="hint">尚无调用记录</span>'}</div><div class="usage-chart">${svg}</div><details class="overview-chart-note" ${OVERVIEW_NOTE_OPEN?'open':''}><summary>统计说明<span>自 ${esc(start)} 起采集</span></summary><p class="hint">按北京时间自然日汇总。采集前的历史未计入；每次请求尝试计一次，重试与追加请求分别计数。页面读取和本地过滤不计数，${PRODUCT_CONSOLE?'安装版按模型与能力统计，不展示密钥。':'密钥仅展示尾号与指纹。'}</p></details></div>`;
}
function renderBot(){
  const bot=OVERVIEW?.bot||{};
  $('#view').innerHTML=`<div class="card"><h2>自动回复服务</h2><p><span class="badge ${bot.running?'ok':'bad'}">${bot.running?'运行中':'已停止'}</span></p><p class="hint">启动或停止大肥鱼的自动回复。微信账号连接在“微信接入”中管理。</p><div class="row"><button class="act" id="botStart" ${bot.running?'disabled':''}>启动机器人</button><button class="ghost danger" id="botStop" ${!bot.running?'disabled':''}>停止机器人</button></div><p class="hint">${bot.age!=null?'最近心跳：'+bot.age+' 秒前':'尚未取得心跳'}</p></div>`;
  $('#botStart').onclick=()=>setBotRunning('start');$('#botStop').onclick=()=>setBotRunning('stop');
  decorateView();
}

let VIEW_TIMER = null;
let WECHAT_ATTEMPT = null;

async function renderWechat(){
  $('#view').innerHTML = `<div class="card"><h2>微信扫码连接</h2><p class="hint">使用大肥鱼的微信账号扫码，并在手机确认。连接后到“会话与管理员”管理参与会话。</p><div class="row"><button class="act" id="wechatStart">开始扫码</button><button class="ghost" id="wechatRefresh">刷新状态</button></div><p id="wechatState" role="status">正在读取连接状态…</p><img id="wechatQr" class="wechat-qr" alt="微信登录二维码" hidden><p class="hint">已有登录状态会自动恢复；新登录不会自动导入电脑微信历史。</p></div>`;
  const status = async()=>{
    const result = await api('/api/wechat-login'+(WECHAT_ATTEMPT?'?attempt='+encodeURIComponent(WECHAT_ATTEMPT):''));
    if(TAB!=='wechat'||!result.ok)return;
    $('#wechatStart').disabled=!!result.connected;
    $('#wechatState').textContent=result.connected?'已连接：'+result.wxid:WECHAT_ATTEMPT?'请扫码并在手机确认，状态：'+(result.operation?.state||'准备中'):'尚未连接，点击“开始扫码”生成二维码。';
    const qr=$('#wechatQr');qr.hidden=!!result.connected||!WECHAT_ATTEMPT;
    if(!qr.hidden)qr.src='/api/wechat-login/qr?attempt='+encodeURIComponent(WECHAT_ATTEMPT)+'&t='+Date.now();
  };
  $('#wechatQr').onerror=()=>{if($('#wechatQr'))$('#wechatQr').hidden=true;};
  $('#wechatStart').onclick=async()=>{
    const button=$('#wechatStart');button.disabled=true;
    try{const result=await api('/api/wechat-login/start',{});if(!result.ok)return;WECHAT_ATTEMPT=result.operation?.id||null;await status();}catch(error){toast(error.message,true);}finally{if(button.isConnected&& !WECHAT_ATTEMPT)button.disabled=false;}
  };
  $('#wechatRefresh').onclick=()=>status().catch(error=>toast(error.message,true));
  await status();
  if(TAB==='wechat')VIEW_TIMER=setInterval(()=>status().catch(()=>{if($('#wechatState'))$('#wechatState').textContent='连接状态读取失败，请刷新重试。';}),4000);
}

async function bindWeb(){
  const cloud=await api('/api/cloud');if(TAB!=='model'||!cloud.ok||!$('#webEnabled'))return;
  $('#webEnabled').checked=!!cloud.web;
  $('#webState').textContent=cloud.webConfigured?'已启用，使用上方配置的 DeepSeek Key':'未启用，或聊天密钥尚未配置';
  $('#webSave').onclick=async()=>{const result=await api('/api/cloud/save',{web:$('#webEnabled').checked});if(result.ok){toast('联网设置已保存');await bindWeb();}};
  $('#webTest').onclick=async()=>{
    const button=$('#webTest');button.disabled=true;$('#webState').textContent='正在检查联网…';
    try{const result=await api('/api/cloud/test',{});if(TAB==='model'&&$('#webState'))$('#webState').textContent=result.ok?'联网成功，返回 '+result.sourceCount+' 个来源':result.error;}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
}

async function renderVoice(){
  const cloud=await api('/api/cloud');if(TAB!=='voice'||!cloud.ok)return;
  $('#view').innerHTML=`<form id="voiceForm" class="card"><h2>语音服务</h2><p class="hint">语音识别和合成使用阿里云百炼北京地域服务；对话内容仍由 DeepSeek 生成。</p><p class="service-state">${cloud.configured?'百炼密钥已配置':'百炼密钥待配置'}</p><label for="voiceKey">百炼 API Key（留空保留原值）</label><input id="voiceKey" type="password" autocomplete="off" placeholder="填写新的 API Key"><label class="check-label"><input id="voiceInput" type="checkbox" ${cloud.voice?'checked':''}>识别新收到的微信语音</label><label class="check-label"><input id="voiceOutput" type="checkbox" ${cloud.voiceReply?'checked':''}>允许发送语音回复</label><p class="hint">启用后，需要识别的微信语音和需要合成的回复文字会提交给百炼服务。</p><div class="row"><button class="act" type="submit">保存语音设置</button><a href="https://help.aliyun.com/zh/model-studio/get-api-key" target="_blank" rel="noreferrer">获取百炼 API Key ↗</a></div></form><div class="card"><h2>大肥鱼的声音</h2><p>沿用已选定的第二版定制音色：软绵、懒洋洋，带鼻音，咬字松弛，尾音轻拖。</p><p class="hint">被要求用语音时发送语音，也可以自主选择语音表达。收到语音不强制回复语音。短哼唱会尝试无伴奏的旋律表达；暂不支持指定歌曲、完整演唱或伴奏。</p></div>`;
  if(PRODUCT_CONSOLE) configureProductVoice(cloud);
  $('#voiceForm').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;
    try{const result=await api('/api/cloud/save',{apiKey:form.querySelector('#voiceKey').value,voice:form.querySelector('#voiceInput').checked,voiceReply:form.querySelector('#voiceOutput').checked,...(PRODUCT_CONSOLE?{voiceId:form.querySelector('#voiceId').value}: {})});if(result.ok){toast('语音设置已保存');if(TAB==='voice')await renderVoice();}}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
}

async function renderMemory(){
  const result=await api('/api/memory/status');if(TAB!=='memory')return;
  $('#view').innerHTML=`<div class="memory-stats"><div class="card"><span class="hint">经历记录</span><strong id="memoryEvents"></strong></div><div class="card"><span class="hint">知识主题</span><strong id="memoryTopics"></strong></div><div class="card"><span class="hint">待整理经历</span><strong id="memoryPending"></strong></div></div><div class="card"><h2>记忆检索</h2><p class="hint">${PRODUCT_CONSOLE?'仅启用会话新增记忆，检索遵守绑定管理员的会话权限。':'群聊与私聊的原始经历及时保存。'}每天北京时间 04:00 集中整理，未整理内容仍可检索。</p><form id="memorySearch" class="row"><div><label for="memoryQuery">关键词</label><input id="memoryQuery" type="text" placeholder="输入人物、事件、知识或任务关键词"></div><button class="act" type="submit">查找记忆</button></form><div id="memoryResults" aria-live="polite"></div></div><div class="card"><h2>管理员任务摘要</h2><p class="hint">摘要来自记忆整理，任务实际进展以执行记录为准。</p><div id="memoryTasks"></div></div><details class="card"><summary>采集与整理状态</summary><pre id="memoryState"></pre></details>`;
  const update=data=>{
    if(TAB!=='memory'||!$('#memoryEvents'))return;
    $('#memoryEvents').textContent=data.events;$('#memoryTopics').textContent=data.topics??'待统计';$('#memoryPending').textContent=data.pending??'待统计';
    $('#memoryState').textContent=JSON.stringify(data.state,null,2);
    $('#memoryTasks').innerHTML=data.tasks?.length?data.tasks.map(task=>`<article class="memory-entry"><h3>${esc(task.topic)}</h3><pre>${esc(task.body)}</pre></article>`).join(''):'<p class="hint">暂未提取到管理员任务</p>';
  };
  update(result);
  $('#memorySearch').onsubmit=async event=>{
    event.preventDefault();const query=$('#memoryQuery').value,button=event.currentTarget.querySelector('button');button.disabled=true;
    try{const data=await api('/api/memory/search?q='+encodeURIComponent(query));if(TAB!=='memory')return;$('#memoryResults').innerHTML=data.items?.length?data.items.map(item=>`<article class="memory-entry"><h3>${esc(item.topic||'原始经历')}</h3><pre>${esc(item.body||'')}</pre><details><summary>来源与完整记录</summary><pre>${esc(JSON.stringify(item,null,2))}</pre></details></article>`).join(''):'<p class="hint">没有找到相关记录</p>';}catch(error){toast(error.message,true);}finally{if(button.isConnected)button.disabled=false;}
  };
  VIEW_TIMER=setInterval(()=>api('/api/memory/status').then(update).catch(()=>toast('记忆状态读取失败，请稍后重试',true)),30000);
}

const NAV_GROUPS = [
 {name:'运行中心',icon:'◉',items:[['overview','运行概览','查看微信连接、机器人状态与静音时段']]},
 {name:'陪伴管理',icon:'♡',items:[['lists','会话与管理员','设置参与会话、白名单与管理员'],['tuning','互动参数','管理群聊参与、回复方式与宵禁时段'],['persona','人格与成长','管理基础人设、演化人设和教导人设'],['memory','记忆与知识库','检索经历、知识和管理员任务摘要'],['stickers','表情包','浏览、发送和管理现有表情包']]},
 {name:'接入与服务',icon:'◇',items:[['bot','机器人控制','启动、停止与查看自动回复服务'],['wechat','微信接入','管理账号连接与扫码登录'],['model','聊天与联网','统一配置聊天密钥和联网搜索'],['voice','语音服务','管理语音识别、语音回复与服务密钥']]},
 {name:'系统管理',icon:'⚙',items:[['tools','消息与静音','向指定会话发送消息，管理静音状态'],['log','运行日志','查看最近的机器人运行记录'],...(PRODUCT_CONSOLE?[["maintenance","数据与升级","备份、迁移和版本升级"]]:[])]}
];
const NAV_ENTRIES = Object.fromEntries(NAV_GROUPS.flatMap(group=>group.items.filter(([id])=>!id.startsWith('/')).map(([id,title,description])=>[id,{title,description,group:group.name}])));
const MOBILE_NAV = window.matchMedia('(max-width:760px)');
$('#navigation').innerHTML=NAV_GROUPS.map((group,index)=>`<div class="nav-group"><button class="nav-heading" aria-expanded="true" aria-controls="nav-${index}"><span class="nav-icon" aria-hidden="true">${group.icon}</span><span>${group.name}</span><span class="chevron" aria-hidden="true">⌄</span></button><div class="nav-items" id="nav-${index}">${group.items.map(([id,title])=>id.startsWith('/')?`<a href="${id}">${title} ↗</a>`:`<button data-tab="${id}">${title}</button>`).join('')}</div></div>`).join('');
function syncNavigation(){
 document.querySelectorAll('.nav-items').forEach(group=>{const expanded=!MOBILE_NAV.matches||!!group.querySelector(`[data-tab="${TAB}"]`);group.hidden=!expanded;group.previousElementSibling.setAttribute('aria-expanded',String(expanded));});
 document.querySelectorAll('nav [data-tab]').forEach(button=>{const active=button.dataset.tab===TAB;button.classList.toggle('on',active);if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
}
function decorateView(){
 const view=$('#view');if(!view||view.querySelector('.page-heading'))return;
 const entry=NAV_ENTRIES[TAB];if(!entry)return;
 view.insertAdjacentHTML('afterbegin',`<div class="page-heading"><p class="eyebrow">控制台 / ${entry.group}</p><h1>${entry.title}</h1><p>${entry.description}</p></div>`);
 view.querySelectorAll('table').forEach(table=>{if(table.parentElement.classList.contains('table-scroll'))return;const wrap=document.createElement('div');wrap.className='table-scroll';table.replaceWith(wrap);wrap.appendChild(table);});
}
new MutationObserver(decorateView).observe($('#view'),{childList:true});
document.querySelectorAll('.nav-heading').forEach(button=>button.onclick=()=>{const expanded=button.getAttribute('aria-expanded')==='true';if(MOBILE_NAV.matches&&!expanded)document.querySelectorAll('.nav-items').forEach(group=>{group.hidden=true;group.previousElementSibling.setAttribute('aria-expanded','false');});button.setAttribute('aria-expanded',String(!expanded));document.getElementById(button.getAttribute('aria-controls')).hidden=expanded;});
document.querySelectorAll('nav [data-tab]').forEach(button=>button.onclick=()=>{if(location.hash.slice(1)===button.dataset.tab)openTab();else location.hash=button.dataset.tab;});
async function openTab(){
 clearInterval(VIEW_TIMER);VIEW_TIMER=null;
 const aliases={'/login':'wechat','/cloud':'model','/memory':'memory'};
 const id=location.hash.slice(1)||aliases[location.pathname];if(id==='teaching')PERSONA_PANE='taught';TAB=id==='teaching'?'persona':NAV_ENTRIES[id]?id:'overview';syncNavigation();
 $('#view').innerHTML='<div class="loading">正在读取…</div>';
 try{if(!OVERVIEW||TAB==='overview')await loadOverview();if(TAB!=='overview')await render();}catch(error){$('#view').innerHTML='<div class="card">'+esc(error.message)+'</div>';}
}
window.addEventListener('hashchange',openTab);MOBILE_NAV.addEventListener('change',syncNavigation);
Promise.resolve().then(openTab);
setInterval(()=>loadOverview().catch(()=>{$('#badgeBot').textContent='状态读取失败，请稍后重试';}),5000);


(function () {
  const cap = document.getElementById('keyCap');
  if (!cap) return;
  async function poll() {
    const r = await fetch('/api/keycap').then(x => x.json()).catch(() => null);
    const box = document.getElementById('keyCapMsg');
    if (!r || !box) return;
    if (r.running) {
      box.textContent = '抓取中…（' + (r.log || '').split('\n').slice(-2).join(' ') + '）';
      setTimeout(poll, 2000);
    } else if (r.log) {
      box.textContent = '上次抓取：' + (r.log || '').split('\n').slice(-3).join(' ');
    }
  }
  cap.onclick = async () => {
    const box = document.getElementById('keyCapMsg');
    box.textContent = '正在启动（同意 UAC 后切到微信点开任意聊天）…';
    const r = await fetch('/api/keycap', {method: 'POST', body: '{}'}).then(x => x.json()).catch(e => null);
    if (!r || !r.ok) { box.textContent = '启动失败：' + ((r && r.error) || '请求出错'); return; }
    poll();
  };
  poll();
})();
