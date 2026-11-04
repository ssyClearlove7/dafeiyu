// 安装版在同一套控制台中接入模型详细配置和数据运维。
function productField(label,name,value,type='text',hint=''){
  return `<label>${esc(label)}</label><input name="${name}" type="${type}" value="${esc(value)}">${hint?`<p class="hint">${esc(hint)}</p>`:''}`;
}

async function renderProductModels(){
  const c=await api('/api/settings');
  if(TAB!=='model')return;
  $('#view').insertAdjacentHTML('beforeend',`<div class="card"><h2>服务参数说明</h2><p>聊天推荐 DeepSeek；语音识别与语音合成推荐阿里云百炼北京地域。密钥从各自服务商创建，服务费用由使用者承担。</p><p class="hint">服务地址填写完整请求 URL，不是控制台网址；模型名称须与服务商实际开放的模型一致。服务商新建工作空间可能使用专属地域地址，请按服务商控制台提示填写。</p></div><div id="productModels"></div>`);
  const names={chat:'聊天',vision:'图片理解',asr:'语音识别',tts:'语音合成'};
  const protocols={chat:['chat_completions'],vision:['chat_completions'],asr:['chat_audio','openai_transcription'],tts:['dashscope_tts','openai_speech']};
  $('#productModels').innerHTML=Object.entries(c.models).map(([k,m])=>`<form class="card" data-service="${k}"><h2>${names[k]} · 详细配置</h2><label>接口格式</label><select name="protocol">${protocols[k].map(v=>`<option ${v===m.protocol?'selected':''}>${v}</option>`).join('')}</select>${productField('服务地址','url',m.url,'url','填写完整请求地址，地址和密钥必须属于同一地域。')}${productField('模型名称','model',m.model,'text',k==='chat'?'推荐 DeepSeek 当前开放的 deepseek-flash。':k==='asr'?'推荐百炼 qwen3-asr-flash，接口格式选择 chat_audio。':k==='tts'?'当前默认使用 cosyvoice-v3-flash，须填写同账号创建且匹配该模型的音色。':'可以使用 DeepSeek 当前支持图片输入的模型；以服务商能力清单为准。')}${productField('API Key（留空保留原值）','key','','password')}${k==='tts'?productField('音色标识','voice',m.voice,'text','填写你自己百炼账号下可用的 Voice ID，不能直接使用卖家的定制音色 ID。'):''}<div class="row"><div>${productField('超时（秒）','timeout',m.timeout,'number')}</div><div>${productField('失败重试次数','retries',m.retries,'number')}</div></div><button class="act" type="submit">保存${names[k]}配置</button></form>`).join('');
  document.querySelectorAll('[data-service]').forEach(f=>f.onsubmit=async event=>{
    event.preventDefault();const b=f.querySelector('button');b.disabled=true;
    try{const d=Object.fromEntries(new FormData(f));d.timeout=Number(d.timeout);d.retries=Number(d.retries);const patch={models:{[f.dataset.service]:d}};if(f.dataset.service==='chat'&&d.key)patch.web={key:d.key};const v=await api('/api/settings/save',patch);if(v.error)return;toast('服务参数已保存');await renderModel();}catch(e){toast(e.message,true);}finally{if(b.isConnected)b.disabled=false;}
  });
}

function configureProductVoice(cloud){
  $('#voiceKey').insertAdjacentHTML('afterend',productField('语音合成音色标识（Voice ID）','voice',cloud.voiceId||'','text','需来自你自己的百炼账号，并匹配合成模型。未配置音色时先不要启用语音回复。').replace('<input name="voice"','<input id="voiceId" name="voice"'));
  const cards=$('#view').querySelectorAll('.card');
  if(cards[1])cards[1].innerHTML='<h2>大肥鱼的声音</h2><p>使用你配置的模型与音色合成语音。对方要求用文字时以文字为先，收到语音不强制回复语音。</p><p class="hint">百炼音色通常绑定创建它的账号和模型；教程中的示例不是可直接复用的专属音色。合成模型、接口和超时在“聊天与联网”的详细配置中设置。</p>';
}

async function productJob(job,target){
  const box=$(target);box.textContent='正在执行…';
  for(let n=0;n<900;n++){
    const v=await api('/api/jobs?id='+encodeURIComponent(job.id));
    box.textContent=v.title+' · '+v.state+(v.error?'：'+v.error:'');
    if(v.state==='completed')return v.result;
    if(['failed','interrupted'].includes(v.state))throw Error(v.error||'任务未完成');
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw Error('任务仍在执行，请稍后查询');
}

async function productUpload(file){
  if(!file)throw Error('请选择文件');
  if(!CONSOLE_CSRF){const s=await api('/api/console/state');CONSOLE_CSRF=s.csrf;}
  const response=await fetch('/api/archive/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':CONSOLE_CSRF},body:file});
  const value=await response.json();if(!response.ok)throw Error(value.error||'上传失败');return value.upload;
}

async function renderMaintenance(){
  const [c,backups]=await Promise.all([api('/api/settings'),api('/api/backups')]);
  if(TAB!=='maintenance')return;
  $('#view').innerHTML=`<div class="card"><h2>备份与时间设置</h2><form id="maintenanceSettings">${productField('时区','timezone',c.timezone)}${productField('每日记忆整理时间','memory_time',c.memory.time,'time')}${productField('每日自动备份时间','backup_time',c.backup.time,'time')}${productField('自动备份保留份数','keep',c.backup.keep,'number')}${productField('备份目录（留空使用默认）','directory',c.backup.directory)}${productField('控制台绑定的本人微信 ID','console_actor',c.console_actor,'text','必须属于“会话与管理员”中的管理员名单。决定可以检索的私聊与群聊范围。')}<button class="act">保存维护设置</button></form><p class="hint">当前数据目录请在运行日志和导入结果中核对。备份失败或缓存清理未通过时不会显示成功。</p><button class="ghost" id="backupNow">立即备份</button><pre id="backupResult"></pre><div>${backups.items.map(r=>`<p><a href="/api/archive/download?file=${encodeURIComponent(r.id+'.dafy')}">${esc(r.reason)} · ${esc(r.created)}</a></p>`).join('')||'<p class="hint">暂无登记备份</p>'}</div></div><div class="card"><h2>换机迁移</h2><p class="hint">导出后旧机暂停，新机使用空数据目录导入并核对，不能同时运行两个副本。</p>${productField('迁移包密码（至少8位）','exportPassword','','password')}<button class="ghost" id="exportData">导出迁移包</button><pre id="exportResult"></pre><label>选择迁移包</label><input id="migrationFile" type="file" accept=".dafy">${productField('迁移包密码','importPassword','','password')}${productField('新数据目录完整路径','destination','')}<button class="ghost" id="importData">校验并导入</button><button class="act" id="activateData" hidden>启用已校验的新目录并重启</button><pre id="importResult"></pre></div><div class="card"><h2>版本升级</h2><p class="hint">首次安装包和签名更新 ZIP 用途不同。先备份，再选择对应系统的完整签名更新包。</p><button class="ghost" id="checkVersion">检查更新</button><input id="versionFile" type="file" accept=".zip"><button class="act" id="installVersion">安装更新包</button><button class="ghost" id="rollbackVersion">回退上一程序版本</button><pre id="versionResult"></pre></div>`;
  const bind=(id,handler)=>$(id).onclick=async()=>{const b=$(id);b.disabled=true;try{await handler();}catch(e){toast(e.message,true);}finally{if(b.isConnected)b.disabled=false;}};
  $('#maintenanceSettings').onsubmit=async e=>{e.preventDefault();try{const d=Object.fromEntries(new FormData(e.target));const result=await api('/api/settings/save',{timezone:d.timezone,console_actor:d.console_actor,memory:{time:d.memory_time},backup:{time:d.backup_time,keep:Number(d.keep),directory:d.directory}});if(!result.error)toast('已保存');}catch(x){toast(x.message,true);}};
  bind('#backupNow',async()=>{await productJob(await api('/api/backups/create',{}),'#backupResult');toast('备份完成');await renderMaintenance();});
  bind('#exportData',async()=>{const result=await productJob(await api('/api/migration/export',{password:document.querySelector('[name=exportPassword]').value}),'#exportResult');$('#exportResult').innerHTML=`旧机已暂停。<a href="/api/archive/download?file=${encodeURIComponent(result.file)}">下载迁移包</a>`;});
  bind('#importData',async()=>{const upload=await productUpload($('#migrationFile').files[0]);const job=await api('/api/migration/import',{upload,password:document.querySelector('[name=importPassword]').value,destination:document.querySelector('[name=destination]').value});const result=await productJob(job,'#importResult');$('#importResult').textContent='已校验并导入：'+result.destination;$('#activateData').hidden=false;bind('#activateData',async()=>{await api('/api/migration/activate',{job:job.id});toast('正在重启，请稍后刷新');});});
  bind('#checkVersion',async()=>{$('#versionResult').textContent=JSON.stringify(await api('/api/updates'),null,2);});
  bind('#installVersion',async()=>{const upload=await productUpload($('#versionFile').files[0]);await productJob(await api('/api/updates/install',{upload}),'#versionResult');toast('更新完成，正在重启');});
  bind('#rollbackVersion',async()=>{if(!confirm('确认保留当前数据并回退程序版本？'))return;await productJob(await api('/api/updates/rollback',{}),'#versionResult');toast('正在切换版本');});
}
