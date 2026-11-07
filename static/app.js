const $ = (s) => document.querySelector(s);
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, body) => {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
};
let state = {projects:[],models:[],connection:{},harnesses:[]};
let selectedProject = localStorage.getItem('polyagent-project') || null;
let view = 'chat';
let lastEvent = 0;
let events = [];
let chats = [];
let conversations = [];
let selectedConversation = null;
let modesKey = '';
let eventGeneration = 0;
const conversationKey = () => `polyagent-chat:${selectedProject}`;
let sending = false;
let sendingTarget = null;
let interrupting = new Set();

function notice(message){const el=$('#notice');el.textContent=message;el.hidden=!message;}
function project(){return state.projects.find(p=>p.id===selectedProject);}
function modelName(id){return state.models.find(m=>m.id===id)?.display_name || id || 'Not selected';}
function conversation(){return conversations.find(c=>c.id===selectedConversation);}
function chatHarness(){const p=project();const current=chats.filter(c=>c.conversation_id===selectedConversation&&c.role==='orchestrator').at(-1);const assigned=p&&(p.mode==='manual'||p.mode==='ask_every_time'||p.mode==='hybrid'&&p.locked_orchestrator);const model=assigned?p.orchestrator_model:current?.model;return state.models.find(m=>m.id===model)?.harness||current?.harness;}
function harnessName(id){return state.harnesses?.find(h=>h.id===id)?.name||{codex:'Codex',antigravity:'Antigravity',deepseek_harness:'DeepSeek Harness',opencode:'OpenCode',grok_build:'Grok Build'}[id]||id;}
function modeDescription(cap,field,value){const option=cap[field.key]?.find(o=>o.value===value);return option?.description||field.description||'';}
function modeFields(harness,values,attribute,started=false){
  const cap=state.harness_modes?.[harness]||{};
  return (cap.fields||[]).filter(f=>cap[f.key]?.length>1).map(f=>{
    const available=cap[f.key],value=available.some(o=>o.value===values[f.key])?values[f.key]:'harness';
    const locked=started&&f.creation_only;
    const help=locked?`${f.label} is fixed for this chat. Start a new chat to change it.`:modeDescription(cap,f,value);
    return `<label class="native-mode-field">${escapeHtml(f.label)}<select ${attribute}="${escapeHtml(f.key)}" data-mode-harness="${escapeHtml(harness)}" ${locked?'disabled':''} title="${escapeHtml(help)}">${available.map(o=>`<option value="${escapeHtml(o.value)}" title="${escapeHtml(o.description||'')}" ${o.value===value?'selected':''}>${escapeHtml(o.label)}</option>`).join('')}</select><small data-mode-help>${escapeHtml(help)}</small></label>`;
  }).join('');
}
function renderConversations(){
  const select=$('#conversation-select');
  select.innerHTML=conversations.length?conversations.map(c=>`<option value="${escapeHtml(c.id)}">${escapeHtml(c.title)}</option>`).join(''):'<option value="">New chat</option>';
  select.value=selectedConversation||'';
  const h=chatHarness(),c=conversation(),cap=state.harness_modes?.[h]||{};
  const values=c?.settings?.[h]||cap.last_used||{};
  const started=chats.some(chat=>chat.conversation_id===selectedConversation&&chat.harness===h);
  const key=JSON.stringify([selectedProject,selectedConversation,h,values,started,cap]);
  if(key===modesKey)return;
  modesKey=key;
  $('#harness-mode-controls').innerHTML=h?`<span class="mode-harness">${escapeHtml(harnessName(h))}</span>${modeFields(h,values,'data-chat-mode',started)}`:project()?.mode==='full_auto'||project()?.mode==='hybrid'&&!project()?.locked_orchestrator?'<small>Auto Chooser uses the saved settings for each harness.</small><button type="button" class="subtle" data-view="harnesses">Harness settings</button>':'';
  if(cap.approval_note)$('#harness-mode-controls').insertAdjacentHTML('beforeend',`<small>${escapeHtml(cap.approval_note)}</small>`);
}
function setView(name){view=name;document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==`view-${name}`);document.querySelectorAll('.nav-button').forEach(el=>el.classList.toggle('active',el.dataset.view===name));const names={chat:['PROJECT CHAT','Chat'],projects:['WORKSPACE','Projects'],harnesses:['CONNECTIONS','Harnesses'],models:['MODEL LIBRARY','Models & research']};$('#view-eyebrow').textContent=names[name][0];$('#view-title').textContent=names[name][1];render();}
async function refresh(){state=await api('/api/state');if(selectedProject&&!project()){selectedProject=null;localStorage.removeItem('polyagent-project');}if(!selectedProject&&state.projects.length){selectedProject=state.projects[0].id;localStorage.setItem('polyagent-project',selectedProject);}render();await loadEvents(true);}
function render(){renderSidebar();renderProjects();renderHarnesses();renderModels();renderChatShell();}
function renderSidebar(){
  $('#project-list').innerHTML=state.projects.map(p=>`<button class="project-item ${p.id===selectedProject?'selected':''}" data-project="${p.id}">${escapeHtml(p.name)}</button>`).join('');
  const connected=Object.values(state.connections||{}).filter(c=>c.running&&c.authenticated).length,found=(state.harnesses||[]).filter(h=>h.selected).length;$('#connection-dot').classList.toggle('ok',connected>0);$('#connection-label').textContent=connected?`${connected} harness${connected===1?'':'es'} connected`:`${found} harness${found===1?'':'es'} found`;
  $('#top-context').textContent=project()?.name || 'No project selected';
}
function renderProjects(){
  $('#project-cards').innerHTML=state.projects.length?state.projects.map(p=>`<div class="card"><h3>${escapeHtml(p.name)}</h3><p>${escapeHtml(p.path)}</p><div class="card-foot"><span class="badge">${escapeHtml(p.mode.replaceAll('_',' '))}</span><button class="subtle" data-select-project="${p.id}">Open project</button></div></div>`).join(''):'<div class="muted">No projects yet.</div>';
  const p=project(), panel=$('#project-config');panel.hidden=!p;if(!p)return;
  const options=state.models.filter(m=>m.available).map(m=>`<option value="${escapeHtml(m.id)}">${escapeHtml(m.display_name)} · ${escapeHtml(m.harness)}</option>`).join('');
  panel.innerHTML=`<h2>Model settings · ${escapeHtml(p.name)}</h2><form id="config-form"><div class="field-grid"><div class="field"><label for="mode">Selection mode</label><select id="mode" name="mode"><option value="manual">Manual</option><option value="full_auto">Full Auto</option><option value="hybrid">Hybrid</option><option value="ask_every_time">Ask Every Time</option></select><small>Antigravity, OpenCode V2 foreground, and Grok Build native subagent calls can be routed through PolyAgent. Codex native calls are observation only.</small></div><div class="field"><label for="orchestrator-model">Orchestrator model</label><select id="orchestrator-model" name="orchestrator_model"><option value="">Select model</option>${options}</select></div><div class="field"><label for="subagent-model">Subagent model</label><select id="subagent-model" name="subagent_model"><option value="">Select model</option>${options}</select><small>Used for Manual and locked Hybrid subagent calls.</small></div><div class="field"><label for="chooser-model">Auto Chooser model</label><select id="chooser-model" name="chooser_model"><option value="">Select model</option>${options}</select><small>Used for Full Auto, unlocked Hybrid assignments, and fresh model research.</small></div><div class="field"><label><input type="checkbox" name="locked_orchestrator" style="width:auto;margin-right:8px">Lock orchestrator in Hybrid</label><small>Use the selected orchestrator model when Hybrid is active.</small></div><div class="field"><label><input type="checkbox" name="locked_subagent" style="width:auto;margin-right:8px">Lock subagent in Hybrid</label><small>Use the selected subagent model when Hybrid is active.</small></div></div><div class="panel-actions"><button class="primary" type="submit">Save model settings</button><span>Manual model changes apply on the next turn.</span></div></form>`;
  $('#mode').value=p.mode;$('#orchestrator-model').value=p.orchestrator_model||'';$('#chooser-model').value=p.chooser_model||'';$('#subagent-model').value=p.subagent_model||'';panel.querySelector('[name="locked_orchestrator"]').checked=!!p.locked_orchestrator;panel.querySelector('[name="locked_subagent"]').checked=!!p.locked_subagent;
}
function renderHarnesses(){
  const list=[...(state.harnesses||[])].sort((a,b)=>Number(!!b.selected)-Number(!!a.selected));
  $('#harness-list').innerHTML=list.map(h=>{
    const c=(state.connections||{})[h.id]||{},connected=c.running&&c.authenticated;
    const status=connected?'Connected':h.selected==='cli'?'CLI detected':h.selected==='app'?'App detected':'Not installed';
    const cli=h.cli?`CLI: ${escapeHtml(h.cli.path)}${h.cli.version?` · ${escapeHtml(h.cli.version)}`:''}`:h.cli_error?`CLI failed: ${escapeHtml(h.cli_error)}`:'CLI not found';
    const app=h.app?`App: ${escapeHtml(h.app.name)} ${escapeHtml(h.app.version||'')} · ${escapeHtml(h.app.path)}`:'App not found';
    const detail=connected?`${escapeHtml(h.name)} connected through ${escapeHtml(c.source||'')} · ${c.model_count} models in catalog.`:h.connectable?'Connect to discover available models.':h.selected?'Installation detected.':'No supported local installation found.';
    const action=h.selected&&h.connectable?`<button class="primary harness-connect" data-connect="${escapeHtml(h.id)}">${connected?'Reconnect':`Connect ${escapeHtml(h.selected==='app'?'app':'CLI')}`}</button>`:'';
    const cap=state.harness_modes?.[h.id],settings=cap&&connected?`<div class="harness-defaults" data-harness-defaults="${escapeHtml(h.id)}">${modeFields(h.id,cap.last_used||{},'data-harness-mode')}<button class="subtle" data-save-harness="${escapeHtml(h.id)}">Save defaults</button><small>Remembers your selection for new chats. Use harness settings follows the harness configuration.${cap.native_defaults?` Current native defaults: ${escapeHtml(Object.values(cap.native_defaults).filter(Boolean).join(' · '))}.`:''}</small></div>`:'';
    return `<div class="panel harness-panel"><div class="harness-top"><div class="harness-icon">${escapeHtml(h.name.charAt(0))}</div><div><h3>${escapeHtml(h.name)}</h3><p>${escapeHtml(h.selected==='cli'?'CLI preferred':h.selected==='app'?'Desktop app installed':'Local discovery')}</p></div><span class="badge ${connected?'good':h.selected?'warn':''}">${status}</span></div>${action}<p class="muted harness-source">${cli}<br>${app}</p><p class="muted">${detail}</p>${settings}</div>`;
  }).join('');
}
function renderModels(){
  $('#model-list').innerHTML=state.models.length?state.models.map(m=>{
    const r=m.research,o=m.override||{};const evidence=(r?.evidence||[]).map(e=>`<div>${escapeHtml(e.type)} · <a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.source)}</a> · ${escapeHtml(e.finding)}</div>`).join('');
    return `<article class="model-card"><div class="model-top"><div><h3>${escapeHtml(m.display_name)}</h3><p>${escapeHtml(m.id)} · ${escapeHtml(m.harness)}</p></div><span class="badge ${m.enabled?'good':''}">${m.enabled?'Enabled':'Disabled'}</span></div><p class="model-description">${escapeHtml(m.metadata.description||'No description supplied by harness.')}</p><div class="model-research"><strong>Research</strong>${r?`<p><b>Strengths:</b> ${escapeHtml(r.strengths.join('; ')||'No substantiated findings')}</p><p><b>Weaknesses:</b> ${escapeHtml(r.weaknesses.join('; ')||'No substantiated findings')}</p><p>${escapeHtml(r.conclusion)}</p>${evidence}<p class="muted">Updated ${new Date(r.updated_at*1000).toLocaleString()}</p>`:'<p>No research stored yet.</p>'}</div><div class="model-actions"><button class="subtle" data-research="${escapeHtml(m.id)}">Fresh research</button><button class="subtle" data-edit-model="${escapeHtml(m.id)}">Edit assessment</button><button class="subtle" data-toggle-model="${escapeHtml(m.id)}">${m.enabled?'Disable':'Enable'}</button></div><div class="model-edit" id="edit-${escapeHtml(m.id)}" hidden><label>Additional strengths<textarea name="strengths">${escapeHtml(o.strengths||'')}</textarea></label><label>Additional weaknesses<textarea name="weaknesses">${escapeHtml(o.weaknesses||'')}</textarea></label><label>Personal assessment<textarea name="assessment">${escapeHtml(o.assessment||'')}</textarea></label><label>Weakness handling<select name="weakness_policy"><option value="normal">Consider normally</option><option value="downweight">Downweight</option><option value="ignore">Ignore</option></select></label><label>Preference<select name="preference"><option value="neutral">Neutral</option><option value="prefer">Prefer</option><option value="strongly_prefer">Strongly prefer</option><option value="force">Force</option><option value="exclude">Exclude</option></select></label><button class="primary" data-save-model="${escapeHtml(m.id)}">Save assessment</button></div></article>`;
  }).join(''):'<div class="muted">Connect a supported harness to discover models.</div>';
  state.models.forEach(m=>{const panel=document.getElementById(`edit-${m.id}`);if(panel){panel.querySelector('[name="weakness_policy"]').value=m.override?.weakness_policy||'normal';panel.querySelector('[name="preference"]').value=m.override?.preference||'neutral';}});
}
function renderChatShell(){const p=project();$('#chat-empty').hidden=!!p;$('#chat-workspace').hidden=!p;if(!p)return;$('#chat-project-name').textContent=p.name;$('#chat-project-path').textContent=p.path;$('#composer-model').textContent=p.mode==='full_auto'||p.mode==='hybrid'&&!p.locked_orchestrator?`Full Auto · chooser: ${modelName(p.chooser_model)}`:`Orchestrator: ${modelName(p.orchestrator_model)}`;renderConversations();}
function renderAgents(){
  const panel=$('#agent-controls');
  const current=chats.filter(c=>c.conversation_id===selectedConversation);
  panel.hidden=!current.length;
  if(!current.length){panel.innerHTML='';return;}
  const running=current.filter(c=>c.status==='running');
  const agents=current.map((c,index)=>{
    const label=c.role==='orchestrator'?'Orchestrator':`Subagent ${current.slice(0,index+1).filter(item=>item.role==='subagent').length}`;
    const active=c.status==='running';
    return `<article class="agent-card ${active?'is-running':''}"><div class="agent-card-main"><span class="agent-role">${label}</span><span class="agent-status ${escapeHtml(c.status)}">${escapeHtml(c.status||'idle')}</span></div><div class="agent-model" title="${escapeHtml(c.model)}">${escapeHtml(modelName(c.model))}</div><div class="agent-card-foot"><span>${escapeHtml(harnessName(c.harness))}</span>${active?`<button class="interrupt-button" type="button" data-interrupt="${escapeHtml(c.id)}" ${interrupting.has(c.id)?'disabled':''}>${interrupting.has(c.id)?'Stopping…':'Interrupt'}</button>`:''}</div></article>`;
  }).join('');
  panel.innerHTML=`<div class="agent-controls-head"><div><strong>Agents</strong><span>${running.length} running · ${current.length} in this chat</span></div>${running.length>1?`<button class="interrupt-button interrupt-all" type="button" data-interrupt-all ${interrupting.has('all')?'disabled':''}>${interrupting.has('all')?'Stopping…':'Interrupt all'}</button>`:''}</div><div class="agent-grid">${agents}</div>`;
}
function inlineMarkdown(text){
  const pattern=/\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|<kbd>([^<]+)<\/kbd>|\*([^*\n]+)\*/g;
  let html='', last=0, match;
  while((match=pattern.exec(text))){
    html+=escapeHtml(text.slice(last,match.index));
    if(match[1]){
      const label=inlineMarkdown(match[1]), url=match[2].trim();
      if(/^https?:\/\//i.test(url)) html+=`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      else if(/^file:\/\//i.test(url)){
        try{const parsed=new URL(url);html+=(!parsed.host||parsed.host==='localhost')?`<button type="button" class="file-link" data-open-file="${escapeHtml(decodeURIComponent(parsed.pathname))}">${label}</button>`:label;}catch{html+=label;}
      }else if(!/^[a-z][a-z0-9+.-]*:/i.test(url)&&!url.startsWith('//')) html+=`<button type="button" class="file-link" data-open-file="${escapeHtml(url)}">${label}</button>`;
      else html+=label;
    }else if(match[3]) html+=`<code>${escapeHtml(match[3])}</code>`;
    else if(match[4]) html+=`<strong>${escapeHtml(match[4])}</strong>`;
    else if(match[5]) html+=`<kbd>${escapeHtml(match[5])}</kbd>`;
    else html+=`<em>${escapeHtml(match[6])}</em>`;
    last=pattern.lastIndex;
  }
  return html+escapeHtml(text.slice(last));
}
function renderMarkdown(text){
  const lines=String(text||'').replace(/\r\n/g,'\n').split('\n');
  let html='', paragraph=[], list=null, code=null;
  const flush=()=>{if(paragraph.length){html+=`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`;paragraph=[];}};
  const closeList=()=>{if(list){html+=`</${list}>`;list=null;}};
  for(const line of lines){
    if(/^\s*```/.test(line)){flush();closeList();if(code!==null){html+=`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`;code=null;}else code=[];continue;}
    if(code!==null){code.push(line);continue;}
    if(!line.trim()){flush();closeList();continue;}
    const heading=line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if(heading){flush();closeList();const level=Math.min(6,heading[1].length+2);html+=`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`;continue;}
    if(/^\s*[-*_]{3,}\s*$/.test(line)){flush();closeList();html+='<hr>';continue;}
    const item=line.match(/^\s*(?:([-*+])\s+|(\d+)[.)]\s+)(.*)$/);
    if(item){flush();const kind=item[1]?'ul':'ol';if(list!==kind){closeList();html+=`<${kind}>`;list=kind;}html+=`<li>${inlineMarkdown(item[3])}</li>`;continue;}
    closeList();paragraph.push(line);
  }
  flush();closeList();if(code!==null)html+=`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`;
  return html;
}
function toolDescription(e){
  const meta=e.metadata||{},detail=meta.detail||{},item=meta.item||{};
  const labels={CORTEX_STEP_TYPE_RUN_COMMAND:'Terminal command',CORTEX_STEP_TYPE_CODE_ACTION:'File change',CORTEX_STEP_TYPE_VIEW_FILE:'Read file',CORTEX_STEP_TYPE_GENERIC:'Agent activity',commandExecution:'Terminal command',fileChange:'File change',mcpToolCall:'Tool call',webSearch:'Web search'};
  const label=labels[e.text]||String(e.text||'Tool activity').replace(/^CORTEX_STEP_TYPE_/,'').replaceAll('_',' ').toLowerCase();
  const phase=String(meta.phase||'').replace(/^CORTEX_STEP_STATUS_/,'').replace('item/','').toLowerCase();
  const detailText=detail.command||detail.file||item.command||item.tool||item.server||'';
  return `<div class="activity tool-activity"><strong>${escapeHtml(label)}</strong>${phase?` · ${escapeHtml(phase)}`:''}${detail.exit_code!==undefined?` · exit ${escapeHtml(detail.exit_code)}`:''}${detailText?`<pre>${escapeHtml(detailText)}</pre>`:''}</div>`;
}
function eventHtml(e){
  const meta=e.metadata||{};
  if(e.kind==='user')return `<div class="message user">${escapeHtml(e.text)}</div>`;
  if(e.kind==='assistant')return `<div class="message assistant"><div class="meta">${chats.find(c=>c.id===e.chat_id)?.role==='subagent'?'Subagent · ':''}${escapeHtml(modelName(meta.model))} · ${escapeHtml(meta.harness||'')}</div><div class="markdown">${renderMarkdown(e.text)}</div></div>`;
  if(e.kind==='choice')return `<div class="activity">Auto Chooser selected ${escapeHtml(modelName(meta.model))}: ${escapeHtml(e.text)}</div>`;
  if(e.kind==='native_subagent')return `<details class="activity native" data-event-id="${escapeHtml(e.id)}"><summary>Native ${escapeHtml(meta.harness||'')} subagent ${escapeHtml(meta.status||'started')}</summary><div class="activity-detail">${escapeHtml(e.text||meta.agent_path||'Delegated prompt unavailable in this event.')}<br><small>${meta.redirected?'PolyAgent routed this call to a new subagent chat.':'Observed; this call was not redirected.'}</small></div></details>`;
  if(e.kind==='subagent_response')return `<div class="message assistant"><div class="meta">Subagent · ${escapeHtml(modelName(meta.model))} · ${escapeHtml(meta.harness||'')}</div><div class="markdown">${renderMarkdown(e.text)}</div></div>`;
  if(e.kind==='model_choice_required')return `<div class="activity approval">Choose a model for this subagent:<br>${escapeHtml(e.text)}<br><select data-model-choice-select="${escapeHtml(meta.request_id)}">${(meta.options||[]).map(id=>`<option value="${escapeHtml(id)}">${escapeHtml(modelName(id))} · ${escapeHtml(id.split(':')[0])}</option>`).join('')}</select><button class="subtle" data-model-choice="${escapeHtml(meta.request_id)}">Use model</button></div>`;
  if(e.kind==='tool')return toolDescription(e);
  if(e.kind==='turn')return `<div class="activity ${e.text==='completed'?'':'error'}">${escapeHtml(meta.harness||'Harness')} turn ${escapeHtml(e.text)} · ${escapeHtml(modelName(meta.model))}</div>`;
  if(e.kind==='error')return `<div class="activity error">${escapeHtml(e.text)}</div>`;
  if(e.kind==='approval')return `<div class="activity approval">${escapeHtml(meta.harness||'Codex')} approval requested: ${escapeHtml(e.text)}<br>${escapeHtml(JSON.stringify(meta.params||{}))}<br><button class="subtle" data-approval="${escapeHtml(meta.request_id)}" data-decision="accept">Approve</button><button class="subtle" data-approval="${escapeHtml(meta.request_id)}" data-decision="decline">Decline</button></div>`;
  if(e.kind==='user_input'){const questions=(meta.params?.questions||[]).map(q=>{const choices=q.options||[];const field=choices.length?`<select name="${escapeHtml(q.id)}" required><option value="">Choose...</option>${choices.map(o=>`<option value="${escapeHtml(o.label)}">${escapeHtml(o.label)}</option>`).join('')}</select>`:`<input name="${escapeHtml(q.id)}" type="${q.isSecret?'password':'text'}" required>`;return `<label>${escapeHtml(q.question)}${field}</label>`;}).join('');return `<form class="activity approval question-form" data-question="${escapeHtml(meta.request_id)}">${questions}<button class="subtle" type="submit">Answer Codex</button></form>`;}
  if(e.kind==='subagent_started')return `<details class="activity" data-event-id="${escapeHtml(e.id)}"><summary>Subagent started · ${escapeHtml(modelName(meta.model))}</summary><div class="activity-detail">${escapeHtml(e.text)}</div></details>`;
  return '';
}
function renderEvents(){const el=$('#chat-events');if(!el)return;const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<80;const previousTop=el.scrollTop;const expanded=new Set(Array.from(el.querySelectorAll?.('details[data-event-id][open]')||[],d=>d.dataset.eventId));el.innerHTML='';const streams=new Map();const visible=events.filter(e=>e.conversation_id===selectedConversation);const resolved=new Set(visible.filter(e=>e.kind==='request_resolved').map(e=>e.metadata?.request_id));const answers=new Set(visible.filter(e=>e.kind==='assistant').map(e=>JSON.stringify([e.chat_id,e.text])));for(const e of visible){if(e.kind==='delta'){const previous=streams.get(e.chat_id)||'';streams.set(e.chat_id,previous+e.text);continue;}if(e.kind==='assistant'){streams.delete(e.chat_id);}if(e.kind==='subagent_response'&&e.metadata?.subagent_chat_id&&answers.has(JSON.stringify([e.metadata.subagent_chat_id,e.text])))continue;if(['approval','user_input','model_choice_required'].includes(e.kind)&&resolved.has(e.metadata?.request_id))continue;const html=eventHtml(e);if(html)el.insertAdjacentHTML('beforeend',html);}for(const [chat,text] of streams){if(text&&chats.find(c=>c.id===chat)?.status==='running')el.insertAdjacentHTML('beforeend',`<div class="message streaming" data-chat="${escapeHtml(chat)}"><div class="meta">LIVE · ${escapeHtml(modelName(chats.find(c=>c.id===chat)?.model))}</div>${escapeHtml(text)}</div>`);}if(sendingTarget?.project===selectedProject&&sendingTarget?.conversation===selectedConversation)el.insertAdjacentHTML('beforeend',`<div class="activity pending" role="status">${sendingTarget.auto?'Choosing an orchestrator model…':'Starting the orchestrator…'}</div>`);for(const detail of el.querySelectorAll?.('details[data-event-id]')||[])detail.open=expanded.has(detail.dataset.eventId);if(nearBottom)el.scrollTop=el.scrollHeight;else el.scrollTop=previousTop;}
async function loadEvents(reset=false){if(!selectedProject)return;const pid=selectedProject;if(reset){eventGeneration++;lastEvent=0;events=[];chats=[];conversations=[];selectedConversation=localStorage.getItem(conversationKey());}const generation=eventGeneration;try{const data=await api(`/api/projects/${pid}/events?since=${lastEvent}`);if(pid!==selectedProject||generation!==eventGeneration)return;events=[...new Map([...events,...data.events].map(e=>[e.id,e])).values()].sort((a,b)=>a.id-b.id);chats=data.chats;conversations=data.conversations||[];if(!conversations.some(c=>c.id===selectedConversation)){selectedConversation=conversations.at(-1)?.id||null;if(selectedConversation)localStorage.setItem(conversationKey(),selectedConversation);}if(data.events.length)lastEvent=Math.max(lastEvent,data.events.at(-1).id);renderConversations();renderAgents();renderEvents();}catch(err){notice(err.message);}}
async function act(fn){try{notice('');await fn();}catch(err){notice(err.message);}}
function openProjectDialog(){$('#project-dialog').showModal();}
document.addEventListener('change',e=>{
  const select=e.target.closest('[data-mode-harness]');if(!select)return;
  const cap=state.harness_modes?.[select.dataset.modeHarness]||{};
  const field=cap.fields?.find(f=>f.key===(select.dataset.chatMode||select.dataset.harnessMode));
  if(field){const help=modeDescription(cap,field,select.value);select.title=help;select.closest('label').querySelector('[data-mode-help]').textContent=help;}
});
document.addEventListener('click',e=>{
  const nav=e.target.closest('[data-view]');if(nav){setView(nav.dataset.view);return;}
  const projectButton=e.target.closest('[data-project],[data-select-project]');if(projectButton){selectedProject=projectButton.dataset.project||projectButton.dataset.selectProject;localStorage.setItem('polyagent-project',selectedProject);lastEvent=0;events=[];setView('chat');loadEvents(true);return;}
  if(e.target.closest('#new-project-side,#new-project-main,#empty-create')){openProjectDialog();return;}
  if(e.target.closest('#chat-settings')){setView('projects');return;}
  const openFile=e.target.closest('[data-open-file],#open-project-folder');if(openFile){act(async()=>{await api(`/api/projects/${selectedProject}/open-file`,{path:openFile.dataset.openFile||''});});return;}
  if(e.target.closest('#new-chat')){act(async()=>{const c=await api(`/api/projects/${selectedProject}/conversations`,{});selectedConversation=c.id;localStorage.setItem(conversationKey(),c.id);await loadEvents();});return;}
  const interrupt=e.target.closest('[data-interrupt]');if(interrupt){const id=interrupt.dataset.interrupt;act(async()=>{interrupting.add(id);renderAgents();try{await api(`/api/projects/${selectedProject}/interrupt/${encodeURIComponent(id)}`,{});await loadEvents();}finally{interrupting.delete(id);renderAgents();}});return;}
  if(e.target.closest('[data-interrupt-all]')){act(async()=>{interrupting.add('all');renderAgents();try{const result=await api(`/api/projects/${selectedProject}/interrupt-all`,{conversation_id:selectedConversation});await loadEvents();if(result.errors?.length)notice(result.errors.map(item=>item.error).join('; '));}finally{interrupting.delete('all');renderAgents();}});return;}
  if(e.target.closest('[data-close]')){$('#project-dialog').close();return;}
  const connect=e.target.closest('[data-connect]');if(connect){act(async()=>{connect.disabled=true;try{await api(`/api/connect/${connect.dataset.connect}`,{});await refresh();}finally{connect.disabled=false;}});return;}
  const saveHarness=e.target.closest('[data-save-harness]');if(saveHarness){act(async()=>{const panel=saveHarness.closest('[data-harness-defaults]'),values={};panel.querySelectorAll('[data-harness-mode]').forEach(el=>values[el.dataset.harnessMode]=el.value);await api(`/api/harnesses/${saveHarness.dataset.saveHarness}/settings`,values);await refresh();});return;}
  if(e.target.closest('#rescan-harnesses')){act(refresh);return;}
  const research=e.target.closest('[data-research]');if(research){act(async()=>{if(!selectedProject)throw new Error('Select a project first');research.disabled=true;try{await api(`/api/projects/${selectedProject}/research`,{model_id:research.dataset.research});await refresh();}finally{research.disabled=false;}});return;}
  const edit=e.target.closest('[data-edit-model]');if(edit){const p=document.getElementById(`edit-${edit.dataset.editModel}`);p.hidden=!p.hidden;return;}
  const toggle=e.target.closest('[data-toggle-model]');if(toggle){act(async()=>{const m=state.models.find(x=>x.id===toggle.dataset.toggleModel);await api(`/api/models/${encodeURIComponent(m.id)}/enabled`,{enabled:!m.enabled});await refresh();});return;}
  const save=e.target.closest('[data-save-model]');if(save){act(async()=>{const panel=document.getElementById(`edit-${save.dataset.saveModel}`);const value={};for(const name of ['strengths','weaknesses','assessment','weakness_policy','preference'])value[name]=panel.querySelector(`[name="${name}"]`).value;await api(`/api/models/${encodeURIComponent(save.dataset.saveModel)}/override`,value);await refresh();});return;}
  const approval=e.target.closest('[data-approval]');if(approval){act(async()=>{await api(`/api/approvals/${approval.dataset.approval}`,{decision:approval.dataset.decision});approval.closest('.activity').remove();});}
  const choice=e.target.closest('[data-model-choice]');if(choice){act(async()=>{const id=choice.dataset.modelChoice;const model=document.querySelector(`[data-model-choice-select="${id}"]`).value;await api(`/api/model-choices/${id}`,{model});await loadEvents();});}
});
$('#project-form').addEventListener('submit',e=>{e.preventDefault();act(async()=>{const form=e.currentTarget;const data=Object.fromEntries(new FormData(form));const p=await api('/api/projects',data);$('#project-dialog').close();form.reset();selectedProject=p.id;localStorage.setItem('polyagent-project',p.id);await refresh();setView('projects');});});
document.addEventListener('submit',e=>{if(e.target.id==='config-form'){e.preventDefault();act(async()=>{const f=e.target;const data=Object.fromEntries(new FormData(f));data.locked_orchestrator=f.querySelector('[name="locked_orchestrator"]').checked;data.locked_subagent=f.querySelector('[name="locked_subagent"]').checked;await api(`/api/projects/${selectedProject}/config`,data);await refresh();});}else if(e.target.matches('[data-question]')){e.preventDefault();act(async()=>{const f=e.target;const answers=Object.fromEntries(new FormData(f));await api(`/api/questions/${f.dataset.question}`,{answers});await loadEvents();});}});
$('#composer').addEventListener('submit',e=>{e.preventDefault();if(sending)return;const input=$('#message'),message=input.value.trim();if(!message)return;act(async()=>{sending=true;sendingTarget={project:selectedProject,conversation:selectedConversation,auto:project()?.mode==='full_auto'||project()?.mode==='hybrid'&&!project()?.locked_orchestrator};renderEvents();$('#composer button[type="submit"]').disabled=true;try{const settings={};document.querySelectorAll('[data-chat-mode]').forEach(el=>settings[el.dataset.chatMode]=el.value);const result=await api(`/api/projects/${selectedProject}/send`,{message,conversation_id:selectedConversation,settings,settings_harness:chatHarness()||null});selectedConversation=result.conversation_id;localStorage.setItem(conversationKey(),selectedConversation);input.value='';await loadEvents();state.harness_modes= (await api('/api/state')).harness_modes;renderConversations();}finally{sending=false;sendingTarget=null;renderEvents();$('#composer button[type="submit"]').disabled=false;}});});
$('#conversation-select').addEventListener('change',e=>{selectedConversation=e.target.value;localStorage.setItem(conversationKey(),selectedConversation);modesKey='';renderConversations();renderAgents();renderEvents();});
$('#message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#composer').requestSubmit();}});
setInterval(()=>{if(selectedProject&&view==='chat')loadEvents();},1200);
refresh().catch(err=>notice(err.message));
