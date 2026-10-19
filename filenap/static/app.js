'use strict';
const token = document.querySelector('meta[name="filenap-token"]').content;
const el = id => document.getElementById(id);
let state, modelReady = false, selected = new Set(), currentPlan = null, approved = new Set(), activeJob = null, polling = null;
const node = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = text; return n; };
const fmtTime = seconds => new Date(seconds * 1000).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const statusText = value => ({move:'Proposed move',keep:'Keep here',review:'Needs review',ready:'Ready to review',applied:'Used',complete:'Moves complete',partial:'Needs attention',applying:'Interrupted batch',moved:'Moved',undone:'Undone',undo_partial:'Undo needs attention',blocked:'Blocked',pending:'Not moved',intent:'Needs reconciliation',undo_intent:'Needs reconciliation'}[value] || value);
function notice(message) { el('notice').textContent = message || ''; el('notice').hidden = !message; }
async function api(path, body) {
  const options = {headers:{'X-FileNap-Token':token}};
  if (body !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  const response = await fetch('/api/' + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The request failed.');
  return data;
}
function view(name) {
  for (const v of ['folder','plans','review','history']) el(v+'-view').hidden = v !== name;
  document.querySelectorAll('.nav-button').forEach(b => b.classList.toggle('selected', b.dataset.view === (name === 'review' ? 'plans' : name)));
  notice('');
}
async function refresh(keepView=false) {
  try {
    state = await api('state');
    const stillHere = new Set(state.scan.files.map(f=>f.id));
    selected = new Set([...selected].filter(id=>stillHere.has(id)));
    el('loading').hidden=true;
    el('inbox-count').textContent=state.scan.files.length;
    el('plan-count').textContent=state.plans.filter(p=>p.status==='ready').length;
    el('batch-count').textContent=state.batches.length;
    el('folder-name').textContent=state.demo ? 'Example inbox' : state.scan.folder;
    el('folder-context').textContent=state.demo ? 'Fictional files for this demo' : 'Local folder';
    el('folder-description').textContent=state.scan.files.length + ' files in this folder. Choose up to 16 for one plan.';
    renderFiles(); renderPlans(); renderHistory();
    if (!keepView) view('folder');
    const running = state.jobs.find(j=>j.status==='running');
    if (running && !activeJob) watchJob(running);
  } catch (error) { el('loading').hidden=true; notice(error.message); }
}
function updateSelection() {
  el('selection-count').textContent = selected.size + ' selected';
  el('make-plan').textContent = activeJob ? 'Planning locally…' : (selected.size ? 'Make a plan · '+selected.size+' files' : 'Make a plan');
  el('make-plan').disabled = !selected.size || selected.size>16 || !modelReady || !!activeJob;
  el('select-all').checked = !!state.scan.files.length && selected.size === Math.min(16,state.scan.files.length);
}
function renderFiles() {
  const list=el('files'); list.replaceChildren();
  if (!state.scan.files.length) { const empty=node('div','empty'); empty.append(node('h2','', 'No loose files here'),node('p','', 'Your saved plans and move records are in the sidebar.')); list.append(empty); }
  state.scan.files.forEach(file=>{
    const label=node('label','file-row'); const check=node('input'); check.type='checkbox'; check.checked=selected.has(file.id); check.setAttribute('aria-label','Select '+file.name);
    check.addEventListener('change',()=>{ if(check.checked && selected.size>=16){check.checked=false;notice('One plan can include up to 16 files.');return;} check.checked?selected.add(file.id):selected.delete(file.id);notice('');updateSelection(); });
    const copy=node('div');copy.style.minWidth='0'; copy.append(node('div','file-name',file.name),node('div','file-preview',file.has_text?file.preview.replace(/\s+/g,' '):'No readable text preview; needs review.'));
    label.append(check,copy,node('span','file-size',file.size<1024?file.size+' B':(file.size/1024).toFixed(1)+' KB'));list.append(label);
  });
  const skipped=el('skipped');skipped.replaceChildren();
  if(state.scan.skipped.length){const detail=node('details','skipped');detail.append(node('summary','',state.scan.skipped.length+' items left out of this scan')); const ul=node('ul');state.scan.skipped.forEach(f=>ul.append(node('li','',f.name+': '+f.reason)));detail.append(ul);skipped.append(detail);}
  updateSelection();
}
async function connect() {
  el('model-state').textContent='Checking local model';el('model-state').className='model-state';
  try {const data=await api('model');modelReady=data.ready;el('model-state').textContent=data.ready?'Local model ready':'Local model unavailable';el('model-state').className='model-state '+(data.ready?'ready':'offline');
    el('model-details').textContent=data.ready?data.name+' · '+data.details.parameter_size+' · '+data.details.quantization_level+'\n'+data.endpoint+'\nManifest '+data.digest:data.error;
  }catch(error){modelReady=false;el('model-state').textContent='Local model unavailable';el('model-state').className='model-state offline';el('model-details').textContent=error.message;}
  if(state)updateSelection();
}
function renderPlans(){
  const list=el('plans');list.replaceChildren();
  if(!state.plans.length){const empty=node('div','empty');empty.append(node('h2','', 'Your first plan starts in the inbox'),node('p','', 'Choose files and ask the local model to suggest folders.'));list.append(empty);return;}
  state.plans.forEach(plan=>{const card=node('div','plan-card');const text=node('div');text.append(node('h2','',fmtTime(plan.created)),node('p','',plan.items.length+' files · '+plan.items.filter(i=>i.decision==='move').length+' proposed moves · '+statusText(plan.status)));const button=node('button','quiet','Review plan');button.onclick=()=>openPlan(plan.id);card.append(text,button);list.append(card);});
}
function openPlan(id){
  currentPlan=state.plans.find(p=>p.id===id);if(!currentPlan)return;
  approved=new Set(currentPlan.items.filter(i=>i.decision==='move').map(i=>i.id));
  renderReview();view('review');el('main').focus({preventScroll:true});
}
function renderReview(){
  const plan=currentPlan,root=el('plan-review');root.replaceChildren();
  const heading=node('div','review-heading'),title=node('div'); title.append(node('p','context',fmtTime(plan.created)),node('h1','', 'Review this plan'),node('p','description','Check the destination and source quote for each proposed move.')); heading.append(title);
  const action=node('div','review-actions'),count=node('span','muted'),apply=node('button','primary');
  function updateApply(){count.textContent=approved.size+' selected moves';apply.textContent='Apply '+approved.size+' moves';apply.disabled=plan.status!=='ready'||!approved.size;}
  apply.onclick=async()=>{apply.disabled=true;notice('');try{await api('apply',{plan_id:plan.id,ids:[...approved]});await refresh(true);view('history');}catch(error){notice(error.message);updateApply();}}; action.append(count,apply);heading.append(action);root.append(heading);
  if(plan.status!=='ready')root.append(node('p','used-note','This plan has already been used. Its recorded moves are in Move history.'));
  const details=node('details','run-details');details.append(node('summary','', 'Local run details'));details.append(node('p','',plan.model.name+' · '+plan.model.seconds+' seconds · '+plan.model.output_tokens+' output tokens\nManifest '+plan.model.digest));root.append(details);
  plan.items.forEach(item=>{const card=node('article','review-card'),line=node('div','review-line'),check=node('input');check.type='checkbox';check.checked=approved.has(item.id);check.disabled=item.decision!=='move'||plan.status!=='ready';check.setAttribute('aria-label','Approve move for '+item.name);check.onchange=()=>{check.checked?approved.add(item.id):approved.delete(item.id);updateApply();};
    const body=node('div','review-body'),path=node('div','move-path');path.append(node('span','source-name',item.name),node('span','arrow',item.decision==='move'?'→':'·'),node('span','destination',item.decision==='move'?'Sorted / '+item.category:statusText(item.decision)));body.append(path,node('p','reason',item.reason));if(item.evidence)body.append(node('blockquote','',item.evidence));
    const source=node('details','source-details');source.append(node('summary','', 'Read source text'),node('pre','',item.preview||'No readable text preview.'));body.append(source);line.append(check,body,node('span','status '+item.decision,statusText(item.decision)));card.append(line);root.append(card);
  });updateApply();
}
function renderHistory(){
  const list=el('history');list.replaceChildren();
  if(!state.batches.length){const empty=node('div','empty');empty.append(node('h2','', 'No file moves yet'),node('p','', 'Approved batches will appear here with a resume and undo record.'));list.append(empty);return;}
  state.batches.forEach(batch=>{const card=node('article','batch-card'),heading=node('div','batch-heading'),title=node('div');title.append(node('h2','',statusText(batch.status)),node('p','',fmtTime(batch.created)+' · '+batch.moves.length+' approved files'));const actions=node('div','batch-actions');
    async function act(kind,button){button.disabled=true;notice('');try{await api(kind,{batch_id:batch.id});await refresh(true);view('history');}catch(error){notice(error.message);button.disabled=false;}}
    if(['partial','applying'].includes(batch.status)){const resume=node('button','quiet','Resume remaining moves');resume.onclick=()=>act('resume',resume);actions.append(resume);}
    const moved=batch.moves.filter(m=>m.status==='moved').length;
    if(batch.status!=='undone'&&moved){const undo=node('button','quiet',batch.status==='undo_partial'?'Retry undo':'Undo '+moved+' moves');undo.onclick=()=>act('undo',undo);actions.append(undo);}
    heading.append(title,actions);card.append(heading);
    batch.moves.forEach(move=>{const row=node('div','move-log'),body=node('div');body.append(node('span','source-name',move.source),node('span','destination','Sorted / '+move.category));if(move.error)body.append(node('p','error',move.error));row.append(body,node('span','status '+move.status,statusText(move.status)));card.append(row);});list.append(card);
  });
}
function watchJob(job){
  activeJob=job;el('planning').hidden=false;updateSelection();
  if(polling)clearInterval(polling);
  polling=setInterval(async()=>{
    el('elapsed').textContent=Math.max(0,Math.floor(Date.now()/1000-job.created))+' seconds elapsed';
    try {const result=await api('jobs/'+job.id);if(result.status==='running')return;clearInterval(polling);polling=null;activeJob=null;el('planning').hidden=true;await refresh(true);if(result.status==='complete')openPlan(result.result);else notice(result.error||'Planning stopped. You can start a new plan.');updateSelection();}
    catch(error){clearInterval(polling);polling=null;activeJob=null;el('planning').hidden=true;notice(error.message);updateSelection();}
  },1200);
}
document.querySelectorAll('.nav-button').forEach(button=>button.onclick=()=>{view(button.dataset.view);});
el('refresh').onclick=()=>{notice('');refresh(true);};el('reconnect').onclick=connect;el('back-plans').onclick=()=>view('plans');
el('select-all').onchange=event=>{selected=event.target.checked?new Set(state.scan.files.slice(0,16).map(f=>f.id)):new Set();renderFiles();notice('');};
el('make-plan').onclick=async()=>{notice('');el('make-plan').disabled=true;try{const job=await api('plan',{ids:[...selected],categories:el('categories').value.split(',').map(x=>x.trim()).filter(Boolean),goal:el('goal').value});watchJob({...job,created:Date.now()/1000});}catch(error){notice(error.message);updateSelection();}};
refresh().then(connect);
