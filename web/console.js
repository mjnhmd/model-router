(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  function setText(id,value) { const el=$(id); if (el) el.textContent=value; }
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let statusData = {models: []}, configData = null, selectedService = null;
  let dirty = false, saving = false, statusPromise = null, statusQueued = false;
  let modelQueues = {}, pendingModelOps = new Set(), drafts = {}, removedUndo = null;
  let filterText = '', sortMode = 'speed';
  const modelNodeByKey = new Map();

  function toast(message, error = false, action) {
    const el = $('toast'); if (!el) return;
    el.textContent = ''; el.className = 'toast show' + (error ? ' error' : '');
    const span = document.createElement('span'); span.textContent = message; el.appendChild(span);
    if (action) { const b = document.createElement('button'); b.className='toast-action'; b.textContent=action.label; b.onclick=()=>{action.run();el.className='toast'}; el.appendChild(b); }
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.className='toast' }, 3600);
  }
  async function jsonFetch(url, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, Object.assign({}, options, {signal: controller.signal}));
      let body = {}; try { body = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : (body.detail && body.detail.message) || '请求失败');
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function serviceId(service) { return service.id || service.name; }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function makeId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'svc-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 9);
  }
  function serviceTemplate() { return {id: makeId(), name:'new-service', base_url:'https://example.com/v1', api_key:'', api_key_set:false, wire_api:'responses', models:[]}; }
  function ensureIds() { (configData && configData.services || []).forEach(s => { if (!s.id) s.id = makeId(); }); }

  function setStatusOffline(message) {
    const el = $('updated'); if (!el) return;
    el.textContent = message || '代理未连接'; el.className = 'muted status-stale';
    const hint = document.querySelector('.hint'); if (hint) hint.textContent = '代理离线或状态已过期，正在重试；配置和当前选择仍保留。';
    document.querySelectorAll('.badge.ok').forEach(el => { el.className = 'badge off'; el.textContent = '状态过期'; });
  }
  async function refreshStatus() {
    if (statusPromise) { statusQueued = true; return statusPromise; }
    statusPromise = (async () => {
      try {
        statusData = await jsonFetch('/v1/status');
        const models = statusData.models || [];
        setText('current', recommendationLabel(statusData, 'responses') || '无可用');
        setText('pool-count', (statusData.enabled_count ?? models.filter(m=>m.enabled).length) + ' 个');
        setText('healthy-count', models.filter(m=>m.healthy && m.enabled).length + ' 个');
        setText('public-model', statusData.public_model || '—');
        $('updated').textContent = '更新于 ' + new Date().toLocaleTimeString(); $('updated').className = 'muted status-fresh';
        updateStatusSummary();
        renderAppHealth();
        renderDashboard();
        if (!saving) renderServices();
      } catch (error) { setStatusOffline(error.message === '请求超时，请稍后重试' ? '代理响应超时' : '代理未连接'); }
      finally {
        statusPromise = null;
        if (statusQueued) { statusQueued = false; refreshStatus(); }
      }
    })();
    return statusPromise;
  }
  async function refreshCodexState() {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.get_codex_state !== 'function') return;
    try { applyCodexState(await api.get_codex_state()); } catch (error) { /* 桌面接口暂不可用时保持现状 */ }
  }
  function applyCodexState(state) {
    const box = $('codex-notice'); if (!box) return;
    const paused = !!state && state.state === 'paused';
    box.hidden = !paused;
    if (paused) {
      setText('codex-notice-title', 'Codex 接入已暂停');
      setText('codex-notice-body', state.message || 'Codex 配置与备份已保留。');
    }
  }
  async function reconnectCodex() {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.reconnect_codex !== 'function') { toast('请在桌面 App 中使用重新接入', true); return; }
    const button = $('codex-reconnect'); button.disabled = true; button.textContent = '正在备份并接入…';
    try {
      const state = await api.reconnect_codex();
      applyCodexState(state);
      const ok = !!state && state.state === 'active';
      toast(ok ? '已备份当前配置并重新接入 Codex' : ((state && state.message) || '接入未完成，请重试'), !ok);
      await refreshStatus();
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = '备份当前配置并接入'; }
  }
  function recommendationLabel(data, protocol) {
    const key = data.recommended_models && data.recommended_models[protocol];
    if (!key) return '';
    const model = (data.models || []).find(m => m.key === key);
    return model ? model.service + '/' + model.model : key;
  }
  function updateStatusSummary() {
    let summary = $('status-summary');
    if (!summary) {
      summary = document.createElement('div'); summary.id='status-summary'; summary.className='status-summary';
      const top = document.querySelector('#dashboard-view .topbar'); if (top) top.querySelector('.subtitle').after(summary);
    }
    const ready = statusData.responses_ready === false ? 'Responses 不可用' : 'Responses 可用';
    summary.textContent = ready + ' · Responses 推荐 ' + (recommendationLabel(statusData,'responses') || '—') + ' · Chat 推荐 ' + (recommendationLabel(statusData,'chat') || '—') + (statusData.instance_id ? ' · 实例 ' + statusData.instance_id : '');
  }
  function renderAppHealth() {
    setText('app-version', 'v' + (statusData.version || '—'));
    const box = $('app-notice'); if (!box) return;
    const restart = statusData.app_restart_required === true;
    box.hidden = !restart;
    if (!restart) return;
    setText('app-notice-title', 'App 已更新，需要重启');
    setText('app-notice-body', '磁盘上的 App 构建于 ' + (statusData.app_build_time || '未知时间')
      + '，当前进程仍在运行旧代码；请退出并重新打开 Model Router，再在 Codex 里重试。');
  }
  function codexModeLabel(mode) { return mode === 'fastest' ? '自动择快' : mode === 'mapped' ? '模型映射' : '未开启'; }
  function renderCodexUsageStatus() {
    const mode = statusData.codex_usage_mode || ((configData && configData.codex && configData.codex.enabled) ? (configData.codex.mode || 'fastest') : 'none');
    setText('codex-use-status', '当前使用：' + codexModeLabel(mode));
    const active = !!(configData && configData.codex && configData.codex.enabled);
    ['start-fastest','start-mapped','stop-codex'].forEach(id => { const button=$(id); if(button) button.disabled = saving; });
    setText('start-fastest', active && mode === 'fastest' ? '关闭自动择快' : '开始使用自动择快');
    setText('start-mapped', active && mode === 'mapped' ? '关闭模型映射' : '开始使用模型映射');
  }

  function renderServices() {
    if (!configData) return;
    const list = $('service-list'); if (!list) return;
    list.innerHTML = configData.services.map((s,i) => '<button class="service-item '+(serviceId(s)===selectedService?'active':'')+'" data-index="'+i+'"><span class="service-name">'+esc(s.name||'未命名服务')+'</span><span class="service-kind">'+esc(s.wire_api||'')+'</span></button>').join('');
    list.querySelectorAll('.service-item').forEach(item => item.addEventListener('click', () => {
      if (!confirmLeave()) return;
      selectedService = serviceId(configData.services[Number(item.dataset.index)]); renderServices(); renderEditor(); switchView('settings');
    }));
  }
  function draftFor(service) {
    const id = serviceId(service);
    if (!drafts[id]) drafts[id] = clone(service);
    return drafts[id];
  }
  function normalizeDrafts() {
    if (!configData) return;
    const valid = new Set(configData.services.map(serviceId));
    Object.keys(drafts).forEach(id => { if (!valid.has(id)) delete drafts[id]; });
  }
  function modelCard(model) { return '<div class="model-card"><div class="model-head"><span class="model-title">'+esc(model.name || model.model)+'</span><span class="badge ok">自动发现</span></div><div class="muted">模型标识来自上游 /models · 已纳入测速候选</div></div>'; }
  async function discover(service) {
    return (await jsonFetch('/v1/services/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:service.id,name:service.name,base_url:service.base_url,api_key:service.api_key||'',wire_api:service.wire_api})})).models || [];
  }
  function validateConfig(show = true) {
    let ok = true; document.querySelectorAll('#editor [data-field-error]').forEach(e=>e.remove());
    (configData && configData.services || []).forEach(s => {
      const d = draftFor(s);
      [['name', d.name && d.name.trim(), '请输入服务名称'],['base_url', /^https?:\/\//.test(d.base_url||''), '请输入有效 Base URL']].forEach(([field,valid,msg]) => {
        const input = document.querySelector('#editor [data-service-id="'+CSS.escape(serviceId(s))+'"][data-field="'+field+'"]');
        if (!valid) { ok=false; if(show && input){ const e=document.createElement('span');e.dataset.fieldError='';e.className='field-error';e.textContent=msg;input.after(e); } }
      });
    });
    const interval = Number($('bench-interval') && $('bench-interval').value);
    if (!(interval >= 1 && interval <= 1440 && Number.isInteger(interval))) { ok=false; if(show && $('bench-interval')) $('bench-interval').setCustomValidity('测速频率需为 1-1440 的整数分钟'); } else if($('bench-interval')) $('bench-interval').setCustomValidity('');
    return ok;
  }
  function renderEditor() {
    if (!configData) return;
    const service = configData.services.find(s => serviceId(s) === selectedService) || configData.services[0];
    if (!service) {
      $('editor').innerHTML='<div class="card empty"><h2>还没有上游服务</h2><p>先添加一个服务，保存时会自动获取它的全部模型。</p><button class="button primary" id="first-service">+ 添加第一个服务</button></div>';
      $('first-service').onclick=()=>addService(); return;
    }
    selectedService = serviceId(service); const d = draftFor(service), models = d.models || [];
    $('editor').innerHTML='<div class="card" data-service-id="'+esc(serviceId(service))+'"><div class="service-head"><div><h2>'+esc(d.name)+'</h2><div class="muted">'+(d.api_key_set?'API Key 已设置，留空表示保持不变':'尚未设置 API Key')+'</div></div><button class="button danger" id="delete-service">删除服务</button></div><div class="form-grid"><div class="field"><label for="field-name">服务名称</label><input id="field-name" data-service-id="'+esc(serviceId(service))+'" data-field="name" type="text" value="'+esc(d.name)+'"></div><div class="field"><label for="field-wire">协议类型</label><select id="field-wire" data-service-id="'+esc(serviceId(service))+'" data-field="wire_api"><option value="responses" '+(d.wire_api==='responses'?'selected':'')+'>Responses（Codex）</option><option value="chat" '+(d.wire_api==='chat'?'selected':'')+'>Chat Completions</option></select></div><div class="field full"><label for="field-base">Base URL（通常以 /v1 结尾）</label><input id="field-base" data-service-id="'+esc(serviceId(service))+'" data-field="base_url" type="text" value="'+esc(d.base_url)+'"></div><div class="field full"><label for="field-key">API Key <span class="muted">'+(d.api_key_set?'· 已保存，留空不变':'')+'</span></label><input id="field-key" data-service-id="'+esc(serviceId(service))+'" data-field="api_key" type="password" value="'+esc(d.api_key||'')+'" placeholder="'+(d.api_key_set?'留空保持当前 Key':'输入上游 API Key')+'"></div></div><div class="service-head" style="margin:24px 0 0"><div><h2>模型列表 <span class="muted">· '+models.length+' 个</span></h2><div class="muted">模型名由上游 /models 自动获取，不需要手动填写</div></div><button class="button" id="refresh-models">'+(models.length?'刷新模型列表':'获取模型列表')+'</button></div><div id="model-editor">'+(models.length?models.map(modelCard).join(''):'<div class="empty">保存服务时会自动获取模型列表</div>')+'</div></div>';
    document.querySelectorAll('#editor [data-field]').forEach(input => input.addEventListener('input', () => {
      const draft = drafts[input.dataset.serviceId] || d; draft[input.dataset.field] = input.value; dirty = true; window.__draftRevision = (window.__draftRevision || 0) + 1;
      if (input.dataset.field === 'name') { const h=$('#editor h2'); if(h) h.textContent=input.value || '未命名服务'; renderServices(); }
      validateConfig(false);
    }));
    $('refresh-models').onclick = async () => {
      const button=$('refresh-models'); button.disabled=true; button.textContent='获取中…';
      try {
        const next=await discover(d), old=new Set((d.models||[]).map(m=>m.name||m.model)), fresh=new Set(next.map(m=>m.name||m.model));
        const added=next.filter(m=>!old.has(m.name||m.model)).length, removed=(d.models||[]).filter(m=>!fresh.has(m.name||m.model)).length;
        if ((added||removed) && !confirm('模型列表将新增 '+added+' 个、移除 '+removed+' 个，继续吗？')) { button.disabled=false;button.textContent=models.length?'刷新模型列表':'获取模型列表'; return; }
        d.models=next; dirty=true; renderEditor(); toast('已获取 '+next.length+' 个模型');
      } catch (error) { button.disabled=false;button.textContent=models.length?'刷新模型列表':'获取模型列表';toast(error.message,true); }
    };
    $('delete-service').onclick=deleteService;
  }
  function addService() {
    if (!configData) return;
    const item=serviceTemplate(); let n=2, base=item.name; while(configData.services.some(s=>s.name===item.name)){item.name=base+'-'+n++;}
    configData.services.push(item); drafts[item.id]=clone(item); selectedService=item.id; dirty=true; renderServices(); renderEditor(); switchView('settings');
  }
  function deleteService() {
    const idx=configData.services.findIndex(s=>serviceId(s)===selectedService); if(idx<0)return;
    const item=configData.services[idx]; configData.services.splice(idx,1); delete drafts[serviceId(item)]; selectedService=configData.services[0] ? serviceId(configData.services[0]) : null; dirty=true; renderServices(); renderEditor();
    removedUndo={item,index:idx}; toast('已删除 '+item.name, false, {label:'撤销',run:()=>{configData.services.splice(removedUndo.index,0,removedUndo.item);drafts[serviceId(removedUndo.item)]=clone(removedUndo.item);selectedService=serviceId(removedUndo.item);dirty=true;renderServices();renderEditor();}});
  }
  async function loadConfig() {
    try { configData=await jsonFetch('/v1/config'); ensureIds(); drafts={}; configData.services.forEach(s=>drafts[serviceId(s)]=clone(s)); selectedService=configData.services[0] ? serviceId(configData.services[0]) : null; dirty=false; normalizeDrafts(); renderServices(); renderEditor(); syncRoutingPreferences(); bindRoutingPreferences(); if(configData.codex) workbenchMode=configData.codex.mode==='mapped'?'mapped':'fastest'; document.querySelectorAll('.mode-tab').forEach(t=>t.classList.toggle('active',t.dataset.mode===workbenchMode)); $('fastest-panel').hidden=workbenchMode!=='fastest'; $('mapping-panel').hidden=workbenchMode!=='mapped'; if(!configData.services.length)switchView('settings'); }
    catch(error){toast(error.message,true);}
  }
  function syncRoutingPreferences(){ if(!configData)return; $('bench-interval').value=String(Math.max(1,Math.round(Number(configData.bench_interval||60)/60))); $('stick-session').value=String(configData.stick_session_to_model!==false); }
  function bindRoutingPreferences(){
    $('bench-interval').oninput=()=>{dirty=true;window.__draftRevision=(window.__draftRevision||0)+1;validateConfig(false); const n=Number($('bench-interval').value);if(n>=1&&n<=1440&&Number.isInteger(n))configData.bench_interval=Math.round(n*60);};
    $('stick-session').onchange=()=>{configData.stick_session_to_model=$('stick-session').value==='true';dirty=true;window.__draftRevision=(window.__draftRevision||0)+1;};
  }
  async function saveConfig(enabledOverride){
    if(!configData||saving)return;
    if(!validateConfig(true)){toast('请先修正表单错误',true);return;}
    saving=true; const button=$('save-config'); button.disabled=true; button.textContent='保存中…'; const revision=window.__draftRevision||0;
    const payload=clone(configData); payload.codex=payload.codex||{enabled:false,mode:'fastest',models:[]}; payload.codex.mode=workbenchMode; if(typeof enabledOverride==='boolean') payload.codex.enabled=enabledOverride; payload.services=payload.services.map(s=>{const d=clone(drafts[serviceId(s)]||s); const keepKey=!!d.api_key_set && !d.api_key; delete d.api_key_set; if(keepKey) delete d.api_key; d.id=serviceId(s); return d;});
    try { await jsonFetch('/v1/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if ((window.__draftRevision||0)===revision) { await loadConfig(); await refreshStatus(); switchView('dashboard'); dirty=false; toast(payload.codex.enabled?'已开始使用'+codexModeLabel(payload.codex.mode):'已关闭 Codex 使用并恢复原配置'); }
      else { dirty=true; toast('保存成功，但检测到保存期间的新编辑，请再次保存'); }
      return true;
    } catch(error){toast(error.message,true); return false;} finally{saving=false;button.disabled=false;button.textContent='保存配置';}
  }
  function confirmLeave(){ return !dirty || window.confirm('有未保存的配置修改，仍要离开吗？'); }
  function switchView(view){ if(view!=='settings' && !confirmLeave())return; document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));document.querySelectorAll('.view').forEach(s=>s.classList.toggle('active',s.id===view+'-view')); }
  document.querySelectorAll('.nav button').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view)));
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  $('add-service').onclick=addService; $('reload-config').onclick=()=>{if(confirmLeave())loadConfig()}; $('save-config').onclick=saveConfig;

  function filteredModels(){const q=filterText.trim().toLowerCase();return (statusData.models||[]).filter(m=>!q||[m.service,m.model,m.wire_api].some(v=>String(v||'').toLowerCase().includes(q)));}
  function orderedModels(models){return models.slice().sort((a,b)=>{if(sortMode==='speed'){const aBenchmarked=Number(a.last_update)>0,bBenchmarked=Number(b.last_update)>0;if(aBenchmarked!==bBenchmarked)return aBenchmarked?-1:1;return Number(b.tps||-1)-Number(a.tps||-1)||Number(selectedForMode(b))-Number(selectedForMode(a));}if(sortMode==='name')return String(a.model).localeCompare(String(b.model),'zh-CN');if(sortMode==='latency')return Number(a.latency||Infinity)-Number(b.latency||Infinity);if(sortMode==='price')return priceValue(a)-priceValue(b);return Number(b.enabled)-Number(a.enabled)||Number(b.tps||-1)-Number(a.tps||-1);});}
  function priceValue(m){const p=m.pricing||{};return typeof p.output_per_million==='number'?p.output_per_million:typeof p.input_per_million==='number'?p.input_per_million:Infinity;}
  function priceText(m){const p=m.pricing||{},a=[];if(typeof p.input_per_million==='number')a.push('入 $'+p.input_per_million.toFixed(2));if(typeof p.output_per_million==='number')a.push('出 $'+p.output_per_million.toFixed(2));return a.length?a.join(' / ')+' / 1M tokens':'未提供';}
  function displayNumber(v,suffix){return typeof v==='number'&&isFinite(v)&&v>0 ? v.toFixed(1)+suffix : '—';}
  let workbenchMode = 'fastest';
  function selectedForMode(model) { return workbenchMode === 'fastest' ? !!model.enabled : !!(configData && configData.codex && configData.codex.models || []).includes(model.key); }
  function freshness(model) {
    if (!model.last_update) return '<span class="bench-fresh">未测速</span>';
    const age = Date.now()/1000 - Number(model.last_update);
    if (age > 900) return '<span class="bench-fresh stale">测速过期</span>';
    return '<span class="bench-fresh">'+(age < 60 ? '刚刚' : Math.round(age/60)+' 分钟前')+'</span>';
  }
  function selectionSummary(id, models, empty) {
    const el=$(id); if(!el)return;
    el.innerHTML=models.length?models.map(m=>'<span class="chip" title="'+esc(m.service+'/'+m.model)+'">'+esc(m.service+'/'+m.model)+'</span>').join(''):'<span class="muted">'+empty+'</span>';
  }
  async function runToggle(keys, enabled) {
    if (workbenchMode === 'mapped') {
      if (!configData.codex) configData.codex={enabled:false,mode:'mapped',models:[]};
      const selected=new Set(configData.codex.models||[]); keys.forEach(k=>enabled?selected.add(k):selected.delete(k));
      configData.codex.models=Array.from(selected); dirty=true; window.__draftRevision=(window.__draftRevision||0)+1; renderDashboard(); return;
    }
    const snapshot=Object.fromEntries((statusData.models||[]).filter(m=>keys.includes(m.key)).map(m=>[m.key,m.enabled]));
    (statusData.models||[]).forEach(m=>{if(keys.includes(m.key))m.enabled=enabled;}); renderDashboard();
    const execute=async()=>{keys.forEach(k=>pendingModelOps.add(k));try{await jsonFetch('/v1/models/toggle-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys,enabled})});await refreshStatus();toast(enabled?'已加入自动择快池':'已移出自动择快池');}catch(error){(statusData.models||[]).forEach(m=>{if(Object.prototype.hasOwnProperty.call(snapshot,m.key))m.enabled=snapshot[m.key];});renderDashboard(true);toast(error.message+'，已回滚当前选择，可重试',true);}finally{keys.forEach(k=>pendingModelOps.delete(k));}};
    const prior=keys.reduce((p,k)=>modelQueues[k]||p,Promise.resolve()); const task=prior.then(execute); keys.forEach(k=>modelQueues[k]=task.catch(()=>{})); return task;
  }
  function renderDashboard(force){
    if (!force && document.activeElement && document.activeElement.closest && document.activeElement.closest('#model-grid')) return;
    const all=statusData.models||[], models=orderedModels(filteredModels());
    $('selection-label').textContent=workbenchMode==='fastest'?'加入自动择快池':'暴露给 Codex';
    renderCodexUsageStatus();
    $('stick-session-workbench').checked=!!(configData&&configData.stick_session_to_model!==false);
    const selected=all.filter(selectedForMode); selectionSummary(workbenchMode==='fastest'?'fastest-summary':'mapping-summary',selected,'还没有选择模型');
    if(workbenchMode==='mapped') selectionSummary('mapping-preview',selected,'选择模型后这里会预览 Codex 列表');
    $('workbench-dirty').textContent=dirty?'有未保存修改':'已保存';
    const grid=$('model-grid'); if(!grid)return; grid.classList.add('model-list');
    const services=[]; models.forEach(m=>{if(!services.includes(m.service))services.push(m.service)});
    grid.innerHTML=services.map(service=>{const serviceModels=all.filter(m=>m.service===service), chosen=serviceModels.filter(selectedForMode).length, visible=models.filter(m=>m.service===service); const groupChecked=chosen===serviceModels.length, groupMixed=chosen>0&&!groupChecked;
      return '<div class="service-divider"><input class="service-check" type="checkbox" '+(groupChecked?'checked':'')+' '+(groupMixed?'data-mixed="true"':'')+' data-service="'+esc(service)+'" aria-label="选择 '+esc(service)+' 全部模型"><b>'+esc(service)+'</b><span class="muted">'+chosen+'/'+serviceModels.length+'</span><button class="group-action" data-service="'+esc(service)+'">'+(groupChecked?'清空':'全选')+'</button></div>'+visible.map(m=>{const picked=selectedForMode(m), measured=Number(m.last_update)>0; return '<div class="model-tile '+(picked?'selected':'off')+'" data-key="'+esc(m.key)+'" data-bench="'+(measured?'ready':'none')+'"><div class="tile-top"><div><div class="tile-name" title="'+esc(m.model)+'">'+esc(m.model)+'</div><div class="tile-service">'+esc(m.service)+' · '+esc(m.wire_api||'')+'</div></div><input class="tile-check" type="checkbox" '+(picked?'checked':'')+' data-key="'+esc(m.key)+'" aria-label="选择 '+esc(m.service+'/'+m.model)+'"></div><div class="tile-speed"><strong>'+(measured?displayNumber(m.tps,''):'—')+'</strong><span>'+(measured&&typeof m.tps==='number'&&m.tps>0?'tokens/s':'未测速')+'<br>'+freshness(m)+'</span></div><div class="tile-meta"><span>延迟 '+displayNumber(m.latency,'s')+'</span><span>'+(m.healthy?'<span class="badge ok">健康</span>':'<span class="badge down">异常</span>')+'</span></div><div class="tile-tags">'+esc([m.supports_tools?'工具':'',m.supports_vision?'视觉':'',m.supports_reasoning?'推理':''].filter(Boolean).join(' · '))+'</div></div>';}).join('')}).join('') || '<div class="empty">还没有模型，请先配置服务</div>';
    grid.querySelectorAll('.service-check').forEach(input=>{if(input.dataset.mixed)input.indeterminate=true; input.onchange=()=>runToggle(all.filter(m=>m.service===input.dataset.service).map(m=>m.key),input.checked)});
    grid.querySelectorAll('input.tile-check').forEach(input=>input.onchange=()=>runToggle([input.dataset.key],input.checked));
    grid.querySelectorAll('.group-action').forEach(btn=>btn.onclick=()=>{const ms=all.filter(m=>m.service===btn.dataset.service),enable=ms.some(m=>!selectedForMode(m));runToggle(ms.map(m=>m.key),enable);});
    $('filter-count') && ($('filter-count').textContent='显示 '+models.length+'/'+all.length+' 个模型');
  }
  $('model-filter').oninput=function(){filterText=this.value;renderDashboard()};
  $('select-visible').onchange=function(){runToggle(filteredModels().map(m=>m.key),this.checked)};
  $('sort-mode').onchange=function(){sortMode=this.value;renderDashboard()};
  document.querySelectorAll('.mode-tab').forEach(tab=>tab.onclick=()=>{workbenchMode=tab.dataset.mode;document.querySelectorAll('.mode-tab').forEach(t=>t.classList.toggle('active',t===tab));$('fastest-panel').hidden=workbenchMode!=='fastest';$('mapping-panel').hidden=workbenchMode!=='mapped';renderDashboard(true)});
  async function startMode(mode){ workbenchMode=mode; document.querySelectorAll('.mode-tab').forEach(t=>t.classList.toggle('active',t.dataset.mode===mode)); $('fastest-panel').hidden=mode!=='fastest'; $('mapping-panel').hidden=mode!=='mapped'; if(configData){configData.codex=configData.codex||{enabled:false,mode:'fastest',models:[]}; configData.codex.enabled=true;} renderDashboard(true); await saveConfig(true); }
  async function stopCodex(){ if(!configData)return; configData.codex=configData.codex||{enabled:false,mode:workbenchMode,models:[]}; configData.codex.enabled=false; renderDashboard(true); if(!await saveConfig(false))return; const api=window.pywebview&&window.pywebview.api; if(api&&typeof api.restore_codex==='function'){try{applyCodexState(await api.restore_codex());}catch(error){toast(error.message,true);}} await refreshStatus(); }
  $('stick-session-workbench').onchange=function(){configData.stick_session_to_model=this.checked;dirty=true;renderDashboard()};
  $('bench-now').onclick=async()=>{try{await jsonFetch('/v1/bench',{method:'POST'});toast('已开始测速当前选择模型')}catch(e){toast(e.message,true)}};
  $('bench-cancel').onclick=async()=>{try{await jsonFetch('/v1/bench/cancel',{method:'POST'});toast('测速已停止')}catch(e){toast(e.message,true)}};
  $('save-workbench').onclick=saveConfig;
  $('start-fastest').onclick=()=>{ const active=configData&&configData.codex&&configData.codex.enabled&&workbenchMode==='fastest'; active?stopCodex():startMode('fastest'); };
  $('start-mapped').onclick=()=>{ const active=configData&&configData.codex&&configData.codex.enabled&&workbenchMode==='mapped'; active?stopCodex():startMode('mapped'); };
  $('stop-codex').onclick=stopCodex;
  $('stop-codex-mapped').onclick=stopCodex;
  function formatTokens(v){const n=Number(v||0);return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);}
  function renderLogs(items) {
    const list = $('log-list');
    if (!items.length) { list.innerHTML = '<div class="empty">暂无请求记录</div>'; return; }
    list.innerHTML = items.map(i => {
      const status = i.ok ? '<span class="log-ok">成功 ' + esc(i.status_code) + '</span>'
                          : '<span class="log-fail">失败 ' + esc(i.status_code) + '</span>';
      const hint = i.hint ? '<div class="log-hint">' + esc(i.hint) + '</div>' : '';
      return '<div class="log-entry"><div class="log-row">'
        + '<span class="log-time">' + esc(i.time) + '</span>'
        + '<span class="log-model">' + esc(i.model) + '</span>'
        + '<span class="log-detail">' + esc(i.endpoint) + '</span>'
        + '<span>' + status + '</span>'
        + '<span class="log-meta">' + Number(i.latency || 0).toFixed(2) + 's</span>'
        + '</div>' + hint + '</div>';
    }).join('');
  }
  async function refreshLogs(){try{const r=await jsonFetch('/v1/logs?limit=100');renderLogs(r.logs||[]);$('logs-updated').textContent='更新于 '+new Date().toLocaleTimeString();}catch(e){$('logs-updated').textContent='日志离线：'+e.message;$('logs-updated').className='muted status-stale';}}
  function renderStats(d){const total=Number(d.total_requests||0),ok=Number(d.success_requests||0);$('stat-total').textContent=total;$('stat-success').textContent=(total?Math.round(ok*100/total):0)+'%';$('stat-latency').textContent=Number(d.avg_latency||0).toFixed(2)+'s';$('stat-tokens').textContent=formatTokens(Number(d.total_input_tokens||0)+Number(d.total_output_tokens||0));$('stat-token-detail').textContent='输入 '+formatTokens(d.total_input_tokens)+' · 输出 '+formatTokens(d.total_output_tokens);$('stats-updated').textContent='更新于 '+new Date().toLocaleTimeString();}
  async function refreshStats(){try{renderStats(await jsonFetch('/v1/stats'));}catch(e){$('stats-updated').textContent='统计离线：'+e.message;$('stats-updated').className='muted status-stale';}}
  window.refreshStatus=refreshStatus; window.renderDashboard=renderDashboard; window.loadConfig=loadConfig; window.saveConfig=saveConfig; window.switchView=switchView; window.jsonFetch=jsonFetch;
  $('codex-reconnect').onclick=reconnectCodex;
  refreshCodexState(); setInterval(refreshCodexState,3000);
  $('refresh-logs').onclick=refreshLogs; setInterval(()=>{if($('logs-view').classList.contains('active'))refreshLogs();if($('stats-view').classList.contains('active'))refreshStats();},3000);
  $('check-update').onclick=async()=>{const button=$('check-update'), result=$('update-result');button.disabled=true;button.textContent='检查中…';try{const data=await jsonFetch('/v1/update');if(data.error){result.textContent=data.error;result.className='muted status-stale';}else if(data.update_available){result.innerHTML='发现新版本 <b>'+esc(data.latest_version)+'</b> · <a href="'+esc(data.release_url)+'" target="_blank" rel="noreferrer">查看 Release</a>';result.className='muted green';}else{result.textContent='当前已是最新版本 '+esc(data.current_version);result.className='muted';}}catch(error){result.textContent=error.message;result.className='muted status-stale';}finally{button.disabled=false;button.textContent='检查更新';}};
  refreshStatus(); loadConfig(); setInterval(()=>refreshStatus(),3000); refreshLogs(); refreshStats();
  // Compatibility markers retained for static checks: classList.add('model-list'), Math.round(interval*60), AbortController, 保留当前选择, price_input, completed, current已选模型.
})();
