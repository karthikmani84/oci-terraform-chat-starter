(function () {
  'use strict';
  function toTime(v) { if (typeof v === 'number') return v < 1e12 ? v * 1000 : v; const t = Date.parse(v || ''); return Number.isFinite(t) ? t : 0; }
  function controlsFor(c, now) {
    c = c || {}; const p = c.plan || {}; const busy = Boolean(c.busy) || ['planning','applying','generating','importing'].includes(c.stage);
    const reviewed = c.reviewed === true || c.reviewed_revision != null && c.reviewed_revision === c.revision || c.stage === 'reviewed';
    const expires = toTime(p.expires_at); const expired = !expires || expires <= (now == null ? Date.now() : now);
    return {busy, reviewed, expires, expired, destructive: Boolean(p.destructive), canReview: !busy && c.stage === 'review' && !reviewed,
      canPlan: !busy && (c.stage === 'review' && reviewed || c.stage === 'reviewed' || c.stage === 'error' && reviewed || c.stage === 'planned' && expired),
      canApply: !busy && c.stage === 'planned' && Boolean(p.id) && !expired, canResolveDrift: !busy && c.stage === 'drift'};
  }
  function confirmationMatches(p, typed) { return Boolean(p && p.destructive && typeof p.confirmation_phrase === 'string' && p.confirmation_phrase.length && typed === p.confirmation_phrase); }
  function readable(v) { return v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v, null, 2); }
  function summaryCounts(s) {
    if (!s || typeof s !== 'object' || Array.isArray(s)) return null;
    if (s.counts && typeof s.counts === 'object') s = s.counts;
    const get = (ks) => { for (const k of ks) if (typeof s[k] === 'number') return s[k]; return null; };
    const result = {create:get(['create','add','adds','to_add']),update:get(['update','change','changes','to_change']),delete:get(['delete','destroy','destroys','to_destroy']),replace:get(['replace','replacements'])};
    return Object.values(result).some(v => v !== null) ? result : null;
  }
  function reviewVisible(conversation) {
    const c = conversation || {};
    if (['review', 'reviewed'].includes(c.stage)) return true;
    return ['planning', 'drift', 'planned', 'error'].includes(c.stage) && (Boolean(c.diff) || c.reviewed_revision != null);
  }
  function baselineDetails(baseline) {
    if (!baseline || typeof baseline.message !== 'string' || !baseline.message || typeof baseline.address !== 'string' || !baseline.address) return null;
    return {message: baseline.message, address: baseline.address};
  }
  function requestHeaders(config, gatewayCsrf, method, hasBody) {
    const headers = {'Accept':'application/json'};
    if (hasBody) headers['Content-Type'] = 'application/json';
    if ((method || 'GET').toUpperCase() === 'POST') {
      if (config && config.csrf_token) headers['X-Chat-CSRF'] = config.csrf_token;
      if (gatewayCsrf) headers['X-CSRF-TOKEN'] = gatewayCsrf;
    }
    return headers;
  }
  function responseError(status, value) {
    const detail = value && (value.message || value.error || value.detail);
    if (status === 401) return 'Your sign-in is no longer available for this action. Refresh this page and sign in again.';
    if (typeof detail === 'string' && detail) return detail;
    if (status === 403) return 'Your sign-in is no longer available for this action. Refresh this page and sign in again.';
    return 'The action could not be completed. Your request remains saved.';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {controlsFor,confirmationMatches,summaryCounts,toTime,requestHeaders,responseError,baselineDetails,reviewVisible};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const API_BASE = location.pathname.startsWith('/single-chat/') ? '/single-chat' : '';
  const state = {config:null,gatewayCsrf:'',current:null,pending:false,poll:null,history:[],signature:'',disconnected:false,dialogPlanId:null};
  const HISTORY = 'oci.infrastructure.chat.requests.v1', ACTIVE = 'oci.infrastructure.chat.active.v1';
  const labels = {clarify:'Clarifying your request',review:'Review the changes',reviewed:'Ready to plan',planning:'Checking live resources and preparing your plan',drift:'A manual change needs your decision',planned:'Your saved plan is ready',applying:'Applying your saved plan',result:'Request completed',error:'This request needs attention',importing:'Preparing existing resources for management',generating:'Writing the proposed Terraform changes'};
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    for (const [k,v] of Object.entries(attrs || {})) { if(k==='text') n.textContent=v; else if(k==='class') n.className=v; else if(k==='click') n.addEventListener('click',v); else if(v!=null) n.setAttribute(k,v); }
    for (const c of children || []) n.append(typeof c==='string' ? document.createTextNode(c) : c); return n;
  }
  function notice(text) { $('notice').textContent = text || ''; $('notice').hidden = !text; }
  async function api(path, method, body) {
    const headers = requestHeaders(state.config, state.gatewayCsrf, method, body !== undefined);
    let r; try { r=await fetch(API_BASE+path,{method:method||'GET',headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)}); }
    catch(_) { throw new Error('The connection was interrupted. Your saved request remains available. Check the connection and refresh the request.'); }
    let v; try { v=await r.json(); } catch(_) {
      if ([401,403].includes(r.status)) throw new Error(responseError(r.status, null));
      throw new Error('The service returned an unreadable response. Refresh the request to check its saved status.');
    }
    if(!r.ok) throw new Error(responseError(r.status, v));
    if (path === '/api/config' && (method || 'GET').toUpperCase() === 'GET') {
      // Gateway and application CSRF values have different headers and live only in memory.
      state.gatewayCsrf = r.headers.get('X-Codegen-Csrf-Token') || '';
    }
    return v;
  }
  function history() {
    const n=$('history'); n.replaceChildren(); if(!state.history.length) n.append(el('p',{class:'muted small',text:'Your requests appear here.'}));
    for(const h of state.history) n.append(el('button',{type:'button',class:'history-button'+(state.current&&state.current.id===h.id?' active':''),title:h.title,text:h.title,click:()=>load(h.id)}));
  }
  function remember(c) {
    if(!c||!c.id)return; const m=(c.messages||[]).find(m=>m.role==='user'); const title=(c.title||m&&readable(m.content)||'New request').slice(0,110);
    state.history=[{id:c.id,title},...state.history.filter(h=>h.id!==c.id)].slice(0,40);
    try {localStorage.setItem(HISTORY,JSON.stringify(state.history));localStorage.setItem(ACTIVE,c.id);} catch(_){} history();
  }
  function head(kicker,title,detail) { return el('div',{class:'card-head'},[el('div',{class:'kicker',text:kicker}),el('h2',{text:title}),el('p',{text:detail})]); }
  function btn(text,fn,primary,disabled) {const b=el('button',{type:'button',class:'button'+(primary?' primary':''),text,click:fn});b.disabled=Boolean(disabled);return b;}
  function diffBlock(value) {
    const p=el('pre',{class:'code-diff',tabindex:'0','aria-label':'Terraform code diff'});
    for(const line of readable(value).split('\n')) p.append(el('span',{class:'diff-line '+(line.startsWith('+')&&!line.startsWith('+++')?'add':line.startsWith('-')&&!line.startsWith('---')?'remove':line.startsWith('@@')?'heading':''),text:line||' '})); return p;
  }
  function drawDiff(n,d) {
    if(typeof d==='string'){n.append(diffBlock(d));return;}
    const entries=Array.isArray(d)?d.map((v,i)=>[v.file||v.path||`Change ${i+1}`,v.diff||v.patch||v.content||v]):Object.entries(d||{});
    for(const [file,body] of entries)n.append(el('div',{class:'diff-group'},[el('div',{class:'diff-file',text:file}),diffBlock(body)]));
  }
  function drawReview(c,ctl) {
    const n=$('change-card');n.replaceChildren();n.hidden=!reviewVisible(c);if(n.hidden)return;
    n.append(head('Code review','Proposed Terraform changes','Review the difference from the saved configuration. Planning does not apply these changes.'));
    const body=el('div',{class:'card-body'});
    const baseline = baselineDetails(c.baseline);
    if (baseline) body.append(el('div',{class:'import-baseline'},[el('h3',{text:'First-change import baseline'}),el('p',{class:'small',text:baseline.message}),el('p',{class:'small muted'},[el('code',{text:baseline.address})])]));
    if (c.diff) drawDiff(body,c.diff);
    else body.append(el('p',{class:'small',text:'Managed code is unchanged. Review it, then create a Plan to compare live resources with the saved configuration.'}));
    n.append(body);const actions=el('div',{class:'card-actions'});
    if(['review','reviewed','error'].includes(c.stage)){
      actions.append(el('span',{class:'hint',text:ctl.reviewed?'Code review confirmed for this revision.':'You can ask for changes in the chat.'}));
      actions.append(ctl.reviewed?btn(c.stage==='error'?'Retry plan':'Create plan',()=>action('plan',{revision:c.revision}),true,state.pending||!ctl.canPlan):btn('Confirm code review',()=>action('review',{revision:c.revision}),true,state.pending||!ctl.canReview));
    }else actions.append(el('span',{class:'hint',text:'Code revision '+c.revision}));n.append(actions);
  }
  function drawDrift(c,ctl) {
    const n=$('drift-card');n.replaceChildren();n.hidden=c.stage!=='drift'||!c.drift;if(n.hidden)return;
    n.append(head('Manual change detected','Which value should we keep?','Live resources differ from the saved Terraform configuration. Choose how to resolve this before planning.'));
    const body=el('div',{class:'card-body'});drawSummary(body,c.drift.summary||c.drift);n.append(body);
    n.append(el('div',{class:'card-actions'},[btn('Restore the code’s value',()=>action('drift',{choice:'restore_code'}),false,state.pending||!ctl.canResolveDrift),btn('Keep the live value',()=>action('drift',{choice:'keep_live'}),true,state.pending||!ctl.canResolveDrift)]));
  }
  function drawSummary(n,s) {
    const counts=summaryCounts(s);if(counts){const row=el('div',{class:'plan-counts'});for(const[k,v]of Object.entries(counts))if(v!==null)row.append(el('span',{class:'count '+(['delete','replace'].includes(k)?'negative':'positive'),text:`${v} ${k}`}));if(s.counts&&s.counts.import)row.append(el('span',{class:'count',text:s.counts.import+' import'}));n.append(row);}
    const resources=Array.isArray(s)?s:s&&(s.resources||s.changes||s.resource_changes);
    if(Array.isArray(resources)){
      const list=el('ul',{class:'resource-list'});for(const r of resources){const a=r.actions||r.change&&r.change.actions||[];const text=Array.isArray(a)?a.join(' → '):readable(a);const item=el('li',{},[el('span',{class:'resource-action'+(/delete|replace/.test(text)?' destructive':''),text}),el('span',{text:r.address||r.name||readable(r)})]);
        const fields=r.diff||r.fields;if(fields)item.append(el('details',{},[el('summary',{text:'Field changes'}),el('pre',{class:'json-detail',text:readable(fields)})]));list.append(item);}n.append(list);
    }else if(typeof s==='string'||!counts)n.append(el('pre',{class:'json-detail',text:readable(s)}));
  }
  function drawPlan(c,ctl) {
    const n=$('plan-card');n.replaceChildren();n.hidden=!c.plan||!['planned','applying','result'].includes(c.stage);if(n.hidden)return;
    const p=c.plan;n.append(head('Saved plan',c.stage==='result'?'Applied plan':'Review the infrastructure plan','Apply uses this exact saved plan. Editing the request, expiry or detected drift requires a new plan.'));
    const body=el('div',{class:'card-body'},[el('div',{class:'plan-id',text:'Plan ID · '+p.id})]);
    if(p.destructive)body.append(el('div',{class:'plan-warning',text:'This plan deletes or replaces resources. A second confirmation is required.'}));drawSummary(body,p.summary||{});n.append(body);
    if(c.stage==='planned'){
      const actions=el('div',{class:'card-actions'},[el('span',{id:'plan-timer',class:'plan-timer'})]);
      if(ctl.expired)actions.append(btn('Create a fresh plan',()=>action('plan',{revision:c.revision}),true,state.pending||!ctl.canPlan));
      else{const b=btn('Apply saved plan',openApply,true,state.pending||!ctl.canApply);b.id='apply-plan';actions.append(b);}n.append(actions);timer();
    }
  }
  function drawResult(c) {
    const n=$('result-card');n.replaceChildren();n.hidden=c.stage!=='result'||!c.result;if(n.hidden)return;
    n.append(head('Result','Changes applied','The Terraform state and configuration are saved for your next request.'));const b=el('div',{class:'card-body'});if(c.result.message){b.append(el('p',{class:'small',text:c.result.message}));if(c.result.details)b.append(el('details',{},[el('summary',{text:'Terraform result'}),el('pre',{class:'json-detail',text:readable(c.result.details)})]));}else drawSummary(b,c.result);n.append(b);
  }
  function render(c) {
    state.current=c;remember(c);const ctl=controlsFor(c);$('welcome').hidden=Boolean((c.messages||[]).length);$('conversation').hidden=false;
    const signature=JSON.stringify([c.messages,c.stage,c.revision,c.error]);const changed=signature!==state.signature;
    if(changed){const n=$('messages');n.replaceChildren();for(const m of c.messages||[]){if(!['user','assistant'].includes(m.role))continue;const user=m.role==='user';n.append(el('li',{class:'message '+(user?'user':'assistant')},[el('div',{class:'message-heading'},[el('span',{class:'message-avatar','aria-hidden':'true',text:user?'Y':'✳'}),document.createTextNode(user?'You':'Infrastructure assistant')]),el('div',{class:'message-body',text:readable(m.content)})]));}state.signature=signature;}
    $('operation-status').hidden=!ctl.busy;$('operation-text').textContent=c.progress||labels[c.stage]||'Working on your request';$('stage-note').textContent=labels[c.stage]||'Changes are limited to the managed compartment.';
    $('send').disabled=state.pending||ctl.busy;$('message').disabled=ctl.busy;$('new-chat').disabled=state.pending;
    drawReview(c,ctl);drawDrift(c,ctl);drawPlan(c,ctl);drawResult(c);if(c.error)notice(readable(c.error.message||c.error));
    if(changed){const target=ctl.busy?$('operation-status'):c.stage==='planned'?$('plan-card'):c.stage==='drift'?$('drift-card'):c.stage==='result'?$('result-card'):c.stage==='review'||c.stage==='reviewed'?$('change-card'):$('messages').lastElementChild;if(target)target.scrollIntoView({block:'nearest',behavior:'smooth'});}schedulePoll();
  }
  function timer() {
    const c=state.current;if(!c||c.stage!=='planned')return;const ctl=controlsFor(c),n=$('plan-timer'),b=$('apply-plan');
    if(n){const sec=Math.max(0,Math.ceil((ctl.expires-Date.now())/1000));n.textContent=ctl.expired?'Plan expired. Create a fresh plan.':`Expires in ${Math.floor(sec/60)}:${String(sec%60).padStart(2,'0')}`;n.classList.toggle('expired',ctl.expired);}
    if(b)b.disabled=state.pending||!ctl.canApply;if(ctl.expired&&b)drawPlan(c,ctl);
    if(state.dialogPlanId&&(!ctl.canApply||c.plan.id!==state.dialogPlanId))closeDestructive();
  }
  function schedulePoll() {
    clearTimeout(state.poll);if(!state.current||!controlsFor(state.current).busy)return;const id=state.current.id;
    state.poll=setTimeout(async()=>{if(!state.current||state.current.id!==id||state.pending){schedulePoll();return;}try{const c=await api('/api/conversations/'+encodeURIComponent(id));if(state.current&&state.current.id===id){if(state.disconnected)notice('');state.disconnected=false;render(c);}}catch(e){if(!state.disconnected)notice(e.message);state.disconnected=true;if(state.current&&state.current.id===id)schedulePoll();}},state.disconnected?5000:1200);
  }
  async function load(id) {
    if(state.pending)return;clearTimeout(state.poll);state.pending=true;notice('');try{const c=await api('/api/conversations/'+encodeURIComponent(id));state.pending=false;render(c);}catch(e){state.pending=false;notice(e.message);schedulePoll();}
  }
  async function create() {clearTimeout(state.poll);const c=await api('/api/conversations','POST',{});state.signature='';render(c);return c;}
  async function action(name,payload) {
    if(state.pending||!state.current)return;const id=state.current.id;state.pending=true;notice('');render(state.current);
    try{const c=await api('/api/conversations/'+encodeURIComponent(id)+'/'+name,'POST',payload);state.pending=false;render(c);}catch(e){state.pending=false;notice(e.message);
      // Never automatically retry Apply after an uncertain response.
      try{const c=await api('/api/conversations/'+encodeURIComponent(id));if(state.current&&state.current.id===id)render(c);}catch(_){schedulePoll();}}
  }
  function closeDestructive(){state.dialogPlanId=null;$('destructive-input').value='';if($('destructive-dialog').open)$('destructive-dialog').close();}
  function openApply(){const c=state.current;if(state.pending||!controlsFor(c).canApply)return;if(!c.plan.destructive){action('apply',{plan_id:c.plan.id});return;}
    if(!c.plan.confirmation_phrase){notice('The service has not supplied the required confirmation for this destructive plan. Create a fresh plan.');return;}
    state.dialogPlanId=c.plan.id;$('confirmation-phrase').textContent=c.plan.confirmation_phrase;$('destructive-input').value='';$('confirm-destructive').disabled=true;$('destructive-dialog').showModal();$('destructive-input').focus();}
  async function submit(event){event.preventDefault();const message=$('message').value.trim();if(!message||state.pending||state.current&&controlsFor(state.current).busy)return;state.pending=true;$('send').disabled=true;notice('');
    try{if(!state.current)await create();const c=await api('/api/conversations/'+encodeURIComponent(state.current.id)+'/messages','POST',{message});$('message').value='';state.pending=false;render(c);}catch(e){state.pending=false;$('send').disabled=false;notice(e.message);schedulePoll();}}
  $('message-form').addEventListener('submit',submit);
  $('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('message-form').requestSubmit();}});
  $('new-chat').addEventListener('click',async()=>{if(state.pending)return;state.pending=true;notice('');try{await create();}catch(e){notice(e.message);}finally{state.pending=false;if(state.current)render(state.current);$('message').focus();}});
  document.querySelectorAll('[data-prompt]').forEach(n=>n.addEventListener('click',()=>{$('message').value=n.dataset.prompt;$('message').focus();}));
  $('cancel-destructive').addEventListener('click',closeDestructive);$('destructive-dialog').addEventListener('cancel',()=>{state.dialogPlanId=null;});
  $('destructive-input').addEventListener('input',()=>{$('confirm-destructive').disabled=!confirmationMatches(state.current&&state.current.plan,$('destructive-input').value);});
  $('destructive-form').addEventListener('submit',e=>{e.preventDefault();const c=state.current,typed=$('destructive-input').value;if(!controlsFor(c).canApply||!c.plan||c.plan.id!==state.dialogPlanId||!confirmationMatches(c.plan,typed))return;const id=state.dialogPlanId;closeDestructive();action('apply',{plan_id:id,destructive_confirmation:typed});});
  setInterval(timer,1000);
  (async()=>{try{const config=await api('/api/config');state.config=config;$('scope-name').textContent=config.scope&&config.scope.name||'Managed compartment';$('scope-region').textContent=config.scope&&config.scope.region||'';$('user-name').textContent=typeof config.user==='string'?config.user:config.user&&(config.user.name||config.user.email)||'';$('service-status').textContent=['ready','ok'].includes(config.status)?'Ready':config.status||'Connected';
    try{const saved=JSON.parse(localStorage.getItem(HISTORY)||'[]');if(Array.isArray(saved))state.history=saved.filter(h=>h&&typeof h.id==='string'&&typeof h.title==='string').slice(0,40);}catch(_){}
    try{const remote=await api('/api/conversations');const items=Array.isArray(remote)?remote:remote.conversations||[];for(const c of items){if(c&&typeof c.id==='string'&&!state.history.some(h=>h.id===c.id)){const m=(c.messages||[]).find(v=>v.role==='user');state.history.push({id:c.id,title:(c.title||m&&readable(m.content)||'Saved request').slice(0,110)});}}state.history=state.history.slice(0,40);}catch(_){}history();let active='';try{active=localStorage.getItem(ACTIVE)||'';}catch(_){}if(active)await load(active);
  }catch(e){$('service-status').textContent='Connection needed';$('scope-name').textContent='Managed compartment';notice(e.message);}})();
})();
