const $=s=>document.querySelector(s);
// ---- texts: L is the catalog of LANG, inlined by the server (i18n/<lang>.json)
function plural(n){n=Math.abs(n|0);if(LANG==='ru'||LANG==='uk'){if(n%10===1&&n%100!==11)return 'one';if(n%10>=2&&n%10<=4&&!(n%100>=12&&n%100<=14))return 'few';return 'many';}return n===1?'one':'other';}
function t(k,v){let s=L[k];if(s==null)return k;if(typeof s==='object')s=s[plural(v&&v.n)]||s.other||s.many||Object.values(s)[0];return v?s.replace(/\{(\w+)\}/g,(m,x)=>x in v?v[x]:m):s;}
const isUrl=s=>/^https?:\/\/\S+$/i.test(s);
const busy=new Set(),expanded=new Set();let showHidden=false,lastState=null,tab='games',archTimer=null;
let sigGames='',sigJobs='',sigDisks='',holdGames=0;
// Re-rendering a list while a <select> is open (or an input is focused) destroys the element and closes the
// picker on phones, so lists are redrawn only when their data changed and never while they are being used.
function focusWithin(sel){const a=document.activeElement,c=document.querySelector(sel);return !!(a&&c&&a!==document.body&&c.contains(a));}
document.addEventListener('pointerdown',e=>{if(e.target.closest('#games select'))holdGames=Date.now()+6000;},true);
document.addEventListener('focusin',e=>{if(e.target.closest('#games select'))holdGames=Date.now()+6000;});
document.addEventListener('change',e=>{if(e.target.closest('#games select'))holdGames=0;});
document.addEventListener('focusout',e=>{if(e.target.closest('#games select'))holdGames=Math.min(holdGames,Date.now()+400);});
let mediaToken=null,mediaItems=[],mediaFilter='all',viewing=null;
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmt(n){if(n==null)return '?';return n<1e6?(n/1e3).toFixed(0)+' KB':n<1e9?(n/1e6).toFixed(1)+' MB':(n/1e9).toFixed(2)+' GB';}
function when(ts){const d=new Date(ts*1000);return d.toLocaleDateString(LANG)+' '+d.toLocaleTimeString(LANG,{hour:'2-digit',minute:'2-digit'});}
function toast(msg,kind){const el=document.createElement('div');el.className='toast'+(kind?' '+kind:'');el.textContent=msg;$('#toasts').appendChild(el);setTimeout(()=>{el.style.opacity='0';setTimeout(()=>el.remove(),320);},kind==='err'?5000:3200);}
// ---- modal helpers
function openModal(html){const m=$('#modal'),b=$('#mbox');b.innerHTML=html;m.classList.add('on');m.onclick=e=>{if(e.target===m)closeModal();};const f=b.querySelector('input,select,textarea');if(f)setTimeout(()=>f.focus(),60);return b;}
function closeModal(){$('#modal').classList.remove('on');$('#mbox').innerHTML='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeViewer();}});
function ask(o){return new Promise(res=>{const b=openModal(`<h3>${esc(o.title)}</h3>${o.text?`<p>${esc(o.text)}</p>`:''}${o.html||''}`
  +(o.fields||[]).map((f,i)=>f.type==='check'?`<label class="chk"><input id="mf${i}" type="checkbox" ${f.value?'checked':''}>${esc(f.label)}</label>`
   :f.type==='select'?`<label>${esc(f.label)}</label><select id="mf${i}">${f.options.map(op=>`<option value="${esc(op.value)}" ${op.value===f.value?'selected':''}>${esc(op.label)}</option>`).join('')}</select>`
   :`<label>${esc(f.label)}</label><input id="mf${i}" type="${f.type||'text'}" value="${esc(f.value||'')}" placeholder="${esc(f.placeholder||'')}" autocomplete="off" ${f.numeric?'inputmode="numeric"':''}>`).join('')
  +`<div class="btns"><button class="ghost" id="mcancel">${t('btn.cancel')}</button><button id="mok" class="${o.danger?'danger':''}">${esc(o.ok||t('btn.ok'))}</button></div>`);
 const done=v=>{closeModal();res(v);};
 $('#mcancel').onclick=()=>done(null);$('#mok').onclick=()=>done((o.fields||[]).map((f,i)=>{const el=$('#mf'+i);return f.type==='check'?el.checked:el.value;}));
 b.querySelectorAll('input').forEach(inp=>inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&inp.type!=='checkbox')$('#mok').click();}));
 $('#modal').onclick=e=>{if(e.target===$('#modal'))done(null);};});}
async function api(path,body,method){const r=await fetch(path,{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j={};try{j=await r.json();}catch(e){}if(!r.ok)toast(j.error||t('err.http',{code:r.status}),'err');refresh();return j;}
// ---- tabs
function showTab(name){tab=name;$('#pgGames').hidden=name!=='games';$('#pgArch').hidden=name!=='arch';$('#pgMedia').hidden=name!=='media';$('#pgSettings').hidden=name!=='settings';
 [['tabGames','games'],['tabArch','arch'],['tabMedia','media'],['tabSettings','settings']].forEach(([id,k])=>$('#'+id).classList.toggle('on',name===k));
 hdr.classList.remove('hide');window.scrollTo(0,0);
 if(name==='media')mediaEnter();if(name==='arch')loadArchives();if(name==='settings')fillSettings();clearInterval(archTimer);if(name==='arch')archTimer=setInterval(loadArchives,5000);}
$('#tabGames').onclick=()=>showTab('games');$('#tabArch').onclick=()=>showTab('arch');$('#tabMedia').onclick=()=>showTab('media');$('#tabSettings').onclick=()=>showTab('settings');$('#settings').onclick=()=>showTab('settings');
// header slides away when scrolling down on a phone and comes back on scroll up
const hdr=document.querySelector('header');let lastY=0;const mobile=matchMedia('(max-width:560px)');
addEventListener('scroll',()=>{const y=scrollY;if(!mobile.matches){hdr.classList.remove('hide');lastY=y;return;}
 if(y>lastY+8&&y>90)hdr.classList.add('hide');else if(y<lastY-8||y<40)hdr.classList.remove('hide');lastY=y;},{passive:true});
$('#addr').onclick=()=>{const u=lastState&&lastState.urls[0];if(!u)return;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(u).then(()=>toast(t('toast.address_copied'),'ok'),()=>toast(u));else toast(u);};
// ---- downloads / uploads
const diskSel=()=>$('#disk').value||undefined;
function download(u){u=(u||$('#url').value).trim();if(!isUrl(u)){toast(t('err.need_url'),'err');return;}$('#url').value='';
 if(isMegaFolder(u)){megaPick(u);return;}api('/api/download',{url:u,disk:diskSel()});}
const MEGA_RE=/^https?:\/\/(?:www\.)?mega(?:\.co)?\.nz\//i;
function isMegaFolder(u){if(!MEGA_RE.test(u))return false;const h=u.split('#')[1]||'';return /\/folder\//i.test(u.split('#')[0])||/^F!/.test(h);}
async function megaPick(url){const j=await api('/api/mega/list',{url});const fs=(j&&j.files)||[];
 if(!j.ok||!fs.length){$('#url').value=url;if(j.ok)toast(t('mega.empty_folder'),'err');return;}
 if(fs.length===1){const r=await api('/api/mega/download',{url,nodes:[fs[0].h],disk:diskSel()});if(r.ok)toast(t('toast.downloading',{name:fs[0].name}),'ok');return;}
 const b=openModal(`<h3>${t('mega.folder_title')}</h3><p>${esc(j.name||'')} · ${t('common.files_n',{n:fs.length})} · ${fmt(j.total)}</p>`
  +`<div class="row" style="gap:8px"><button class="ghost sm" id="mAll">${t('mega.select_all')}</button><button class="ghost sm" id="mNone">${t('mega.select_none')}</button></div>`
  +`<div class="mlist">${fs.map((f,i)=>`<label class="vn"><input type="checkbox" data-mi="${i}" checked><div style="flex:1;min-width:0"><b>${esc(f.name)}</b><small>${f.dir?esc(f.dir)+' · ':''}${fmt(f.size)}</small></div></label>`).join('')}</div>`
  +`<div class="hint" id="mHint" style="margin-top:8px"></div><div class="btns"><button class="ghost" id="mCancel">${t('btn.cancel')}</button><button id="mOk">${t('add.download')}</button></div>`);
 const boxes=[...b.querySelectorAll('[data-mi]')];
 const upd=()=>{const n=boxes.filter(x=>x.checked).length;$('#mOk').textContent=n?t('mega.download_n',{n}):t('add.download');$('#mOk').disabled=!n;
  $('#mHint').textContent=n>1?t('mega.hint_many',{name:j.name||'Mega'}):n===1?t('mega.hint_one'):'';};
 boxes.forEach(x=>x.onchange=upd);$('#mAll').onclick=()=>{boxes.forEach(x=>x.checked=true);upd();};$('#mNone').onclick=()=>{boxes.forEach(x=>x.checked=false);upd();};
 $('#mCancel').onclick=closeModal;
 $('#mOk').onclick=async()=>{const nodes=boxes.filter(x=>x.checked).map(x=>fs[+x.dataset.mi].h);closeModal();
  const r=await api('/api/mega/download',{url,nodes,disk:diskSel()});if(r.ok)toast(nodes.length>1?t('mega.downloading_folder',{name:j.name||'Mega',n:nodes.length}):t('toast.downloading',{name:(r.jobs[0]||{}).label||''}),'ok');};
 upd();}
$('#go').onclick=()=>download();
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')download();});
$('#clear').onclick=()=>api('/api/clear',{});
$('#stopAll').onclick=async()=>{const n=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;
 const r=await ask({title:t('stop.title'),text:t('stop.text',{n}),ok:t('stop.ok'),danger:true});if(!r)return;
 const j=await api('/api/cancel_all',{});if(j.ok)toast(j.stopped?t('stop.done',{n:j.stopped}):t('stop.nothing'),'ok');};
$('#toggleHidden').onclick=()=>{showHidden=!showHidden;render(lastState);};
$('#disk').onchange=()=>api('/api/settings',{default_disk:$('#disk').value});
function upload(f){const x=new XMLHttpRequest();x.open('PUT','/api/upload/'+encodeURIComponent(f.name)+(diskSel()?'?disk='+encodeURIComponent(diskSel()):''));x.onerror=()=>toast(t('err.upload_named',{name:f.name}),'err');x.onload=refresh;x.send(f);setTimeout(refresh,300);}
$('#file').onchange=e=>{[...e.target.files].forEach(upload);e.target.value='';};
const d=$('#drop');
['dragenter','dragover'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.add('over');}));
['dragleave','drop'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.remove('over');}));
d.addEventListener('drop',e=>{if(e.dataTransfer.files.length){[...e.dataTransfer.files].forEach(upload);return;}
 const u=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split('\n')[0].trim();if(isUrl(u))download(u);});
document.addEventListener('paste',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;const u=(e.clipboardData.getData('text')||'').trim();if(isUrl(u))download(u);});
// ---- game actions (event delegation; lists re-render only when their data changes)
document.addEventListener('change',e=>{const el=e.target;if(el.dataset.compat!==undefined)api('/api/game/compat',{game:el.dataset.game,exe:el.dataset.exe,tool:el.value}).then(j=>{if(j.ok)toast(j.note,'ok');});});
document.addEventListener('click',async e=>{const card=e.target.closest('[data-open]');if(card&&!e.target.closest('button,select,input,a')){openGame(card.dataset.open);return;}
 const b=e.target.closest('button');if(!b)return;const ds=b.dataset;
 if(ds.cancel)api('/api/cancel',{id:+ds.cancel});
 if(ds.pwgo!==undefined){const box=b.closest('.item');const pw=box.querySelector('input[type=password]').value;const rem=box.querySelector('input[type=checkbox]').checked;if(!pw){toast(t('err.enter_password'),'err');return;}api('/api/job/password',{id:+ds.pwgo,password:pw,remember:rem});}
 if(ds.add!==undefined)addFlow(ds.game,ds.add);
 if(ds.rename!==undefined){const r=await ask({title:t('rename.title'),fields:[{label:t('common.name'),value:ds.name}],ok:t('rename.ok')});if(!r)return;const j=await api('/api/game/rename',{game:ds.game,exe:ds.rename,name:r[0]});if(j.ok)toast(j.note,'ok');}
 if(ds.hide!==undefined)api('/api/game/hide',{path:ds.hide,hidden:ds.hidden==='1'});
 if(ds.unimport!==undefined){const r=await ask({title:t('unimport.title'),text:t('unimport.text',{name:ds.name}),ok:t('unimport.ok'),danger:true});if(!r)return;
  const j=await api('/api/game/unimport',{path:ds.unimport});if(j.ok){toast(t('unimport.done',{name:j.removed}),'ok');if(view.kind==='game')closeGame();}}
 if(ds.imppick!==undefined){const j=await api('/api/game/import',{path:ds.imppick});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}}
 if(ds.del!==undefined){const r=await ask({title:t('delete.title'),text:t('delete.game_text',{name:ds.name}),fields:[{label:t('delete.remove_shortcut'),type:'check',value:true},{label:'PIN',type:'password',numeric:true}],ok:t('delete.ok'),danger:true});if(!r)return;
  const j=await api('/api/game/delete',{path:ds.del,pin:r[1],remove_shortcut:r[0]});if(j.ok){toast(t('toast.deleted',{what:j.removed.join(', ')})+(j.notes&&j.notes.length?'. '+j.notes.join('; '):''),'ok');if(view.kind==='game')closeGame();}}
 if(ds.aextract!==undefined)api('/api/archive/extract',{path:ds.aextract}).then(j=>{if(j.id){toast(t('toast.unpacking',{name:j.label}),'ok');showTab('games');}});
 if(ds.adel!==undefined){const r=await ask({title:t('arch.delete_title'),text:ds.name,ok:t('delete.ok'),danger:true});if(!r)return;const j=await api('/api/archive/delete',{path:ds.adel});if(j.ok){toast(t('toast.deleted',{what:j.removed.join(', ')}),'ok');loadArchives();}}
});
const CANCELLABLE=['queued','resolving','downloading'];let lastJobs=[];
function compatOptions(cur){const tools=(lastState&&lastState.compat_tools)||[];let opts=tools.map(x=>({value:x.name,label:x.label}));if(cur&&!opts.some(o=>o.value===cur))opts.unshift({value:cur,label:cur});opts.push({value:'',label:t('compat.none_native')});return opts;}
function compatLabel(v){const tool=((lastState&&lastState.compat_tools)||[]).find(x=>x.name===v);return tool?tool.label:(v||t('compat.none'));}
const chosenExe=g=>g.exes.find(x=>x.in_steam)||g.exes.find(x=>x.recommended)||g.exes[0];
// ---- list <-> game page routing (hash, so the phone's Back button works)
let view={kind:'list',path:null},sigGame='',extrasFor=null,gameInfo=null;
function parseHash(){const m=location.hash.match(/^#g=(.+)$/);return m?{kind:'game',path:decodeURIComponent(m[1])}:{kind:'list',path:null};}
function applyView(v){view=v;sigGames='';sigGame='';extrasFor=null;$('#gameList').hidden=view.kind==='game';$('#gamePage').hidden=view.kind!=='game';if(view.kind==='game'&&tab!=='games')showTab('games');window.scrollTo(0,0);render(lastState);}
// history is an enhancement (phone Back button returns to the list); the view switches even where it is unavailable
function openGame(path){try{history.pushState({g:path},'','#g='+encodeURIComponent(path));}catch(e){}applyView({kind:'game',path});}
function closeGame(){if(history.state&&history.state.g){history.back();return;}try{history.replaceState(null,'',location.pathname+location.search);}catch(e){}applyView({kind:'list',path:null});}
addEventListener('popstate',()=>applyView(parseHash()));
$('#gpBack').onclick=closeGame;
function exeRow(g,x){const k=g.path+'|'+x.exe;let right;
 if(busy.has(k))right=`<span class="pill warn">${t('game.adding')}</span>`;
 else if(x.in_steam)right=`<span class="pill">${t('game.in_steam')}</span>${x.pending?`<span class="pill warn" title="${t('game.pending_title')}">${t('game.pending')}</span>`:''}`+(x.linux?'':`<select class="sel" data-compat data-game="${esc(g.path)}" data-exe="${esc(x.exe)}">${compatOptions(x.compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(x.compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`);
 else right=`<button class="sm" data-add="${esc(x.exe)}" data-game="${esc(g.path)}">${t('game.add_to_steam')}</button>`;
 return `<div class="exe"><div class="exeh"><span class="n">${esc(x.exe)}</span><span class="pill k">${x.linux?'Linux':'Windows'}</span>${x.recommended?`<span class="pill acc">${t('game.recommended')}</span>`:''}<span class="acts">${right}</span></div></div>`;}
function listCard(g){const x=g.exes.find(e=>e.in_steam);
 return `<div class="item gcard${g.hidden?' hid':''}" data-open="${esc(g.path)}"><div class="top"><b>${esc(g.title||g.name)}</b><span class="acts"><span class="pill k">${esc(g.disk)}</span>${g.imported?`<span class="pill acc" title="${t('game.own_title')}">${t('game.own')}</span>`:''}${x?`<span class="pill">${t('game.in_steam')}</span>`:`<span class="pill k">${t('game.not_added')}</span>`}${x&&x.pending?`<span class="pill warn">${t('game.pending')}</span>`:''}${x&&x.art?`<span class="pill">${t('game.has_art')}</span>`:''}<span class="chev">›</span></span></div><div class="path">${esc(g.path)}</div></div>`;}
function renderList(s){lastJobs=s.jobs||[];
 const running=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;$('#stopAll').hidden=!running;$('#stopAll').textContent=running>1?t('jobs.stop_all_n',{n:running}):t('jobs.stop_all');
 const jsig=JSON.stringify(s.jobs);
 if(jsig!==sigJobs&&!focusWithin('#jobs')){sigJobs=jsig;
 $('#jobs').innerHTML=s.jobs.map(j=>{const act=j.status==='downloading'||j.status==='uploading';const pct=j.total?Math.round(j.done*100/j.total):0;
  let st=L['status.'+j.status]?t('status.'+j.status):j.status;if(act)st+=' · '+fmt(j.done)+(j.total?' / '+fmt(j.total)+' · '+pct+'%':'')+(j.speed?' · '+t('unit.per_sec',{size:fmt(j.speed)}):'');
  const cls=j.status==='error'?'err':j.status==='done'?'ok':j.status==='cancelled'?'':'busy';const can=j.kind==='download'&&CANCELLABLE.includes(j.status);
  return `<div class="item"><div class="top"><b>${esc(j.label)}</b><span class="acts"><span class="pill k">${esc(j.disk||'')}</span>${can?`<button class="ghost sm" data-cancel="${j.id}">${t('jobs.cancel')}</button>`:''}</span></div>`
   +(act?`<div class="bar"><i style="width:${pct}%"></i></div>`:'')+`<div class="st ${cls}">${esc(st)}${j.error?' — '+esc(j.error):''}</div>`
   +(j.status==='needs_password'?`<div class="row wrap" style="margin-top:8px"><input type="password" placeholder="${t('jobs.archive_password')}" style="flex:1;min-width:140px;padding:8px 12px;font-size:.95em"><label class="small muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" checked>${t('jobs.remember')}</label><button class="sm" data-pwgo="${j.id}">${t('jobs.unpack')}</button></div>`:'')
   +(j.game_dir?`<div class="path">→ ${esc(j.game_dir)}</div>`:j.status==='done'&&j.file?`<div class="path">→ ${esc(j.file)}</div>`:'')+'</div>';}).join('')||`<div class="empty">${t('jobs.empty')}</div>`;}
 const gsig=JSON.stringify([s.games,showHidden]);
 if(gsig!==sigGames){sigGames=gsig;const nh=s.games.filter(g=>g.hidden).length;$('#toggleHidden').textContent=showHidden?t('games.hide_hidden'):t('games.show_hidden',{n:nh});$('#toggleHidden').hidden=!nh&&!showHidden;
  $('#games').innerHTML=s.games.filter(g=>showHidden||!g.hidden).map(listCard).join('')||`<div class="empty">${t('games.empty')}</div>`;}}
function renderGame(s){const g=(s.games||[]).find(x=>x.path===view.path);
 if(!g){$('#gpHead').innerHTML=`<div class="empty">${t('game.not_found')}</div>`;$('#gpExes').innerHTML='';$('#gpCoversCard').hidden=$('#gpSavesCard').hidden=$('#gpFilesCard').hidden=true;$('#gpActs').innerHTML='';$('#gpInfo').innerHTML='';return;}
 if(extrasFor!==g.path){extrasFor=g.path;loadGameExtras(g);}
 const sig=JSON.stringify([g,[...busy],s.compat_tools,gameInfo]);if(sig===sigGame||focusWithin('#gamePage')||Date.now()<holdGames)return;sigGame=sig;
 const ch=chosenExe(g);const inSteam=g.exes.some(x=>x.in_steam);
 $('#gpHead').innerHTML=`<div class="top"><div style="min-width:0"><div class="gtitle">${esc(g.title||g.name)}${inSteam?`<button class="ghost sm" data-rename="${esc(ch.exe)}" data-game="${esc(g.path)}" data-name="${esc(ch.clean_name||ch.name)}" title="${t('game.rename')}">✎</button>`:''}</div><div class="path">${esc(g.path)}</div></div><span class="acts"><span class="pill k">${esc(g.disk)}</span>${inSteam?`<span class="pill">${t('game.in_steam')}</span>`:`<span class="pill k">${t('game.not_added')}</span>`}${g.imported?`<span class="pill acc" title="${t('game.own_title')}">${t('game.own')}</span>`:''}${g.hidden?`<span class="pill warn">${t('game.hidden')}</span>`:''}</span></div>`;
 $('#gpExes').innerHTML=g.exes.map(x=>exeRow(g,x)).join('')||`<div class="empty">${t('game.no_exes')}</div>`;
 $('#gpCoversCard').hidden=!inSteam;$('#gpSavesCard').hidden=!inSteam;$('#gpFilesCard').hidden=!g.exes.length;
 $('#gpActs').innerHTML=(g.hidden?`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="0">${t('game.unhide')}</button>`:`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="1">${t('game.hide')}</button>`)+(g.imported?`<button class="ghost sm" data-unimport="${esc(g.path)}" data-name="${esc(g.title||g.name)}">${t('unimport.button')}</button>`:'')
  +`<button class="danger sm" data-del="${esc(g.path)}" data-name="${esc(g.title||g.name)}">${t('game.delete')}</button>`
  +(g.imported?`<span class="hint" style="flex-basis:100%">${t('game.own_hint')}</span>`:'');
 const info=[t('info.folder_html',{name:esc(g.name)}),t('info.disk',{disk:esc(g.disk)})];
 if(gameInfo&&gameInfo.path===g.path)info.push(t('info.size',{size:fmt(gameInfo.size),n:gameInfo.files}));
 if(ch&&ch.in_steam){info.push(t('info.steam_name',{name:esc(ch.name)}));if(ch.appid)info.push(`Steam AppID: <code>${ch.appid}</code>`);if(!ch.linux)info.push(`Proton: ${esc(compatLabel(ch.compat))}${ch.compat_from==='steam'?' · '+t('info.from_steam_settings'):''}`);
  if(ch.art_source)info.push(t('info.art',{source:ch.art_source==='vndb'?'VNDB · '+esc(ch.vndb_title||''):ch.art_source==='custom'?t('art.src.custom'):ch.art_source==='steam'?t('art.src.steam'):t('art.src.icon')}));if(ch.art_note)info.push(esc(ch.art_note));if(ch.art_error)info.push(`<span class="err">${esc(ch.art_error)}</span>`);if(ch.pending)info.push(`<span class="busy">${t('info.pending')}</span>`);}
 $('#gpInfo').innerHTML=info.join('<br>');}
function render(s){if(!s)return;
 $('#ver').textContent='v'+s.version;
 const c=s.cdp||{};const cdpTxt=!c.enabled?t('cdp.short.off'):c.available?t('cdp.short.live'):c.marker?t('cdp.short.after_reboot'):t('cdp.short.no_steam');
 $('#ffm').textContent=(s.ffmpeg?t('ffmpeg.found'):t('ffmpeg.missing_long'))+' · '+cdpTxt+(s.pending?' · '+t('footer.pending',{n:s.pending}):'');
 $('#addr').textContent=s.urls[0]||'';$('#addr').title=s.urls.join('\n');
 const dsel=$('#disk');const dsig=JSON.stringify([s.disks,(s.settings||{}).default_disk]);
 if(dsig!==sigDisks&&document.activeElement!==dsel){const cur=dsel.value||(s.settings&&s.settings.default_disk)||'internal';dsel.innerHTML=(s.disks||[]).map(d=>`<option value="${esc(d.id)}" ${d.id===cur?'selected':''}>${esc(d.label)} · ${t('disk.free',{size:fmt(d.free)})}</option>`).join('');dsel.hidden=(s.disks||[]).length<2;sigDisks=dsig;}
 if(view.kind==='game')renderGame(s);else renderList(s);}
// polled every second while the page is visible; a hidden tab (phone locked, another app or tab) asks
// nothing and catches up the moment it is shown again. One request at a time, so a slow Deck is not
// piled on; a refresh asked for meanwhile (e.g. right after an action) runs as soon as it returns.
let polling=false,again=false;
async function refresh(){if(polling){again=true;return;}polling=true;
 try{lastState=await(await fetch('/api/state')).json();render(lastState);}catch(e){}
 finally{polling=false;if(again){again=false;refresh();}}}
function tick(){if(!document.hidden)refresh();}
document.addEventListener('visibilitychange',tick);
applyView(parseHash());refresh();setInterval(tick,1000);
// ---- game page extras: folder size, covers with preview/replace, saves
const LAB={portrait:[t('slot.portrait'),t('slot.portrait_size'),'2/3'],landscape:[t('slot.landscape'),'920×430','920/430'],hero:[t('slot.hero'),'1920×620','1920/620'],logo:[t('slot.logo'),t('slot.logo_size'),'16/5'],icon:[t('slot.icon'),t('slot.icon_size'),'1/1']};
function loadGameExtras(g){gameInfo=null;const ch=chosenExe(g);if(g.exes.length)setupFiles(g);
 fetch('/api/game/info?path='+encodeURIComponent(g.path)).then(r=>r.json()).then(j=>{if(!j.error){gameInfo={path:g.path,...j};sigGame='';render(lastState);}}).catch(()=>{});
 if(ch&&ch.in_steam){loadCovers(g,ch);loadSaves(g,ch);}else{$('#gpCovers').innerHTML='';$('#gpSaves').innerHTML='';}}
// ---- files inside the game: a patch next to the exe, or anywhere below the game folder
let filesTimer=0;
function filesQ(g){const exe=$('#gpFExe').value||((chosenExe(g)||{}).exe)||'';return `game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(exe)}&dir=${encodeURIComponent($('#gpFDir').value.trim())}`;}
function setupFiles(g){const ch=chosenExe(g);
 $('#gpFExe').innerHTML=g.exes.map(x=>`<option value="${esc(x.exe)}" ${ch&&x.exe===ch.exe?'selected':''}>${esc(x.exe)}</option>`).join('');$('#gpFExeRow').hidden=g.exes.length<2;
 $('#gpFDir').value='';$('#gpFWhere').textContent='';$('#gpFList').innerHTML='';
 $('#gpFDir').oninput=()=>{clearTimeout(filesTimer);filesTimer=setTimeout(()=>loadFiles(g),350);};
 $('#gpFExe').onchange=()=>loadFiles(g);
 $('#gpFUp').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.multiple=true;inp.onchange=()=>uploadGameFiles(g,[...inp.files]);inp.click();};
 $('#gpFArch').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='.zip,.7z,.rar,.tar,.tgz,.txz,.tbz2,.gz,.xz,.bz2';inp.onchange=()=>{const f=inp.files[0];if(f)unpackArchive(g,f);};inp.click();};
 loadFiles(g);}
async function loadFiles(g){let j;try{j=await(await fetch('/api/game/dir?'+filesQ(g))).json();}catch(e){j={error:t('err.no_connection')};}
 if(j.error){$('#gpFWhere').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpFList').innerHTML='';$('#gpFUp').disabled=$('#gpFArch').disabled=true;return null;}
 $('#gpFUp').disabled=$('#gpFArch').disabled=false;
 $('#gpFWhere').innerHTML=t('files.where_html',{path:esc(j.game_rel==='.'?t('files.game_root'):j.game_rel)})+(j.exists?'':` · <span class="busy">${t('files.will_create')}</span>`);
 const n=j.entries.length+j.more;
 $('#gpFList').innerHTML=j.exists?(n?`<details class="hint"><summary>${t('files.now_in_folder',{n})}</summary><ul class="list">${j.entries.map(e=>`<li>${e.dir?'📁 ':''}<code>${esc(e.name)}</code>${e.dir?'':' · '+fmt(e.size)}</li>`).join('')}${j.more?`<li>${t('common.and_more',{n:j.more})}</li>`:''}</ul></details>`:`<div class="hint">${t('files.empty_folder')}</div>`):'';
 return j;}
function putGameFile(q,f,replace){return new Promise(res=>{const xh=new XMLHttpRequest();
 xh.open('PUT',`/api/game/file?${q}&name=${encodeURIComponent(f.name)}${replace?'&replace=1':''}`);
 xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=t('files.uploading_progress',{name:f.name,pct:Math.round(e.loaded*100/e.total),done:fmt(e.loaded),total:fmt(e.total)});};
 xh.onload=async()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}
  if(xh.status===409&&r.exists&&!replace){const ok=await ask({title:t('files.replace_title'),text:t('files.replace_text',{name:f.name}),ok:t('files.replace_ok'),danger:true});return res(ok?await putGameFile(q,f,true):false);}
  if(xh.status===200){toast(`${f.name} → ${r.rel}${r.backup?' · '+t('files.backup_kept',{name:r.backup}):''}`,'ok');res(true);}else{toast(r.error||t('err.upload'),'err');res(false);}};
 xh.onerror=()=>{toast(t('err.upload_named',{name:f.name}),'err');res(false);};xh.send(f);});}
async function uploadGameFiles(g,files){if(!files.length)return;const j=await loadFiles(g);if(!j)return;
 const dirs=new Set(j.entries.filter(e=>e.dir).map(e=>e.name)),have=new Set(j.entries.filter(e=>!e.dir).map(e=>e.name));
 const asDir=files.find(f=>dirs.has(f.name));if(asDir){toast(t('files.is_dir',{name:asDir.name}),'err');return;}
 const clash=files.filter(f=>have.has(f.name)).map(f=>f.name);
 if(clash.length){const r=await ask({title:t('files.replace_many_title'),text:t('files.replace_many_text',{names:clash.join(', ')}),ok:t('files.replace_ok'),danger:true});if(!r)return;}
 const q=filesQ(g);$('#gpFUp').disabled=true;let done=0;
 try{for(const f of files){if(!await putGameFile(q,f,clash.includes(f.name)))break;done++;}}finally{$('#gpFUp').disabled=false;}
 if(done>1)toast(t('files.uploaded_n',{n:done}),'ok');loadFiles(g);}
// ---- an archive unpacked over the game: upload, unpack aside, preview, then apply
const ARCH_RE=/\.(zip|7z|rar|tar|tgz|txz|tbz2|tar\.(gz|xz|bz2))$/i;
async function unpackArchive(g,f){
 if(!ARCH_RE.test(f.name)){toast(t('files.not_archive'),'err');return;}
 if(!await loadFiles(g))return;
 $('#gpFUp').disabled=$('#gpFArch').disabled=true;
 try{let r=await new Promise(res=>{const xh=new XMLHttpRequest();xh.open('PUT',`/api/game/archive?${filesQ(g)}&name=${encodeURIComponent(f.name)}`);
   xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=t('files.uploading_pct',{name:f.name,pct:Math.round(e.loaded*100/e.total)});};
   xh.upload.onload=()=>{$('#gpFWhere').textContent=t('patch.unpacking_preview');};
   xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}res(xh.status===200?j:{error:j.error||t('err.http',{code:xh.status})});};
   xh.onerror=()=>res({error:t('err.upload_named',{name:f.name})});xh.send(f);});
  if(r.error&&!r.needs_password){toast(r.error,'err');return;}
  while(r.needs_password){const p=await ask({title:t('patch.pw_title'),text:r.error?t('patch.pw_wrong',{name:r.name}):t('patch.pw_locked',{name:r.name}),fields:[{label:t('common.password'),type:'password'},{label:t('patch.pw_remember'),type:'check',value:true}],ok:t('patch.pw_open')});
   if(!p||!p[0]){api('/api/game/archive/discard',{token:r.token});return;}
   $('#gpFWhere').textContent=t('patch.unpacking');r=await api('/api/game/archive/unlock',{token:r.token,password:p[0],remember:p[1]});if(!r.ok)return;}
  await patchPreview(g,r);
 }finally{$('#gpFUp').disabled=$('#gpFArch').disabled=false;loadFiles(g);}}
function patchPreview(g,r){return new Promise(done=>{
 const where=r.target_rel==='.'?t('files.game_root'):r.target_rel;
 const stat=v=>{let h=t('patch.stat_files',{n:v.files,size:fmt(v.size)})+' '+(v.conflicts?t('patch.stat_replaces',{n:v.conflicts,names:v.sample.map(esc).join(', ')+(v.conflicts>v.sample.length?', …':'')}):t('patch.stat_no_conflicts'));
  if(v.blocked_count)h+=`<br><span class="err">${t('patch.stat_blocked',{n:v.blocked_count,names:v.blocked.map(esc).join(', ')+(v.blocked_count>v.blocked.length?', …':'')})}</span>`;return h;};
 openModal(`<h3>${t('patch.title')}</h3><p>«${esc(r.name)}» → <code>${esc(where)}</code></p>`
  +`<div class="hint">${t('patch.inside')} ${r.top.map(x=>`<code>${esc(x)}</code>`).join(' ')||t('common.empty')}${r.top_more?' '+t('common.and_more',{n:r.top_more}):''}</div>`
  +(r.single_top?`<label class="chk"><input type="checkbox" id="pvStrip">${t('patch.strip',{name:esc(r.single_top),where:esc(where)})}</label>`:'')
  +`<div class="hint" id="pvStat"></div>`
  +`<label class="chk"><input type="checkbox" id="pvBak" checked>${t('patch.backup')}</label>`
  +`<div class="btns"><button class="ghost" id="pvCancel">${t('btn.cancel')}</button><button id="pvOk">${t('patch.ok')}</button></div>`);
 const cur=()=>$('#pvStrip')&&$('#pvStrip').checked?r.stripped:r.plain;
 const upd=()=>{const v=cur();$('#pvStat').innerHTML=stat(v);$('#pvOk').disabled=!v.files;$('#pvOk').className=v.conflicts?'danger':'';
  $('#pvOk').textContent=!v.files?t('patch.nothing'):v.conflicts?t('patch.ok_replace',{n:v.conflicts}):t('patch.ok');};
 if($('#pvStrip'))$('#pvStrip').onchange=upd;upd();
 const cancel=()=>{closeModal();api('/api/game/archive/discard',{token:r.token});done(false);};
 $('#pvCancel').onclick=cancel;$('#modal').onclick=e=>{if(e.target===$('#modal'))cancel();};
 $('#pvOk').onclick=async()=>{const strip=!!($('#pvStrip')&&$('#pvStrip').checked),backup=$('#pvBak').checked;closeModal();
  $('#gpFWhere').textContent=t('patch.applying');
  const a=await api('/api/game/archive/apply',{token:r.token,strip,backup});
  if(a.ok)toast(t('patch.done',{where:a.target_rel==='.'?t('files.game_root'):a.target_rel,n:a.written})+(a.replaced?', '+t('patch.done_replaced',{n:a.replaced}):'')+(a.backups?', '+t('patch.done_backups',{n:a.backups}):'')+(a.skipped_count?', '+t('patch.done_skipped',{n:a.skipped_count}):''),'ok');
  done(!!a.ok);};});}
async function loadCovers(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpCvHint').textContent=t('common.looking');
 $('#gpVndb').onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x));
 $('#gpIcon').onclick=async()=>{const j=await api('/api/art',{game:g.path,exe:x.exe,source:'icon'});if(j.ok){toast(t('covers.drawing_from_icon'),'ok');setTimeout(()=>loadCovers(g,x),5000);}};
 try{const j=await(await fetch('/api/art/current?'+q)).json();if(j.error){$('#gpCvHint').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpCovers').innerHTML='';return;}
  $('#gpCvHint').textContent=(j.source==='vndb'?t('covers.src_vndb',{title:j.vndb_title||''}):j.source==='custom'?t('covers.src_custom'):j.source==='icon'?t('covers.src_icon'):j.source==='steam'?t('covers.src_steam'):t('covers.src_none'))+(j.live?'':' · '+t('covers.after_restart'))+(j.note?' · '+j.note:'')+(j.error?' · '+j.error:'')+'. '+t('covers.replace_hint');
  $('#gpCovers').innerHTML=Object.keys(LAB).map(sl=>{const it=j.slots[sl];const [ttl,sz,ar]=LAB[sl];return `<div class="cov"><div class="im" style="--ar:${ar}">${it?`<img src="${esc(it.url)}" alt="">`:`<span class="muted small">${t('common.none')}</span>`}</div><b>${ttl}</b><small>${sz}${it?' · '+fmt(it.size):''}</small><div class="acts" style="justify-content:center"><button class="ghost sm" data-cup="${sl}">${t('covers.replace')}</button><button class="ghost sm" data-cvn="${sl}">VNDB…</button>${sl==='icon'?`<button class="ghost sm" data-cexe="${sl}">${t('covers.from_exe')}</button>`:''}</div></div>`;}).join('');
  $('#gpCovers').querySelectorAll('[data-cvn]').forEach(bt=>bt.onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x),bt.dataset.cvn));
  $('#gpCovers').querySelectorAll('[data-cexe]').forEach(bt=>bt.onclick=async()=>{bt.disabled=true;bt.textContent=t('covers.taking');
   const jj=await api('/api/art/from_exe',{game:g.path,exe:x.exe,slot:bt.dataset.cexe});if(jj.ok)toast(t('covers.icon_taken',{exe:x.exe,size:jj.size}),'ok');setTimeout(()=>loadCovers(g,x),600);});
  $('#gpCovers').querySelectorAll('[data-cup]').forEach(bt=>bt.onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='image/png,image/jpeg';inp.onchange=()=>{const f=inp.files[0];if(!f)return;bt.disabled=true;bt.textContent=t('common.uploading');
   const xh=new XMLHttpRequest();xh.open('PUT','/api/art/upload?'+q+'&slot='+bt.dataset.cup);xh.onload=()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast(t('covers.replaced'),'ok');else toast(r.error||t('err.upload'),'err');setTimeout(()=>loadCovers(g,x),600);refresh();};xh.onerror=()=>{toast(t('err.upload'),'err');loadCovers(g,x);};xh.send(f);};inp.click();});
 }catch(e){$('#gpCvHint').textContent=t('status.error');}}
async function loadSaves(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpSaves').textContent=t('common.looking');$('#gpSvDl').href='/api/saves/backup?'+q;
 $('#gpSvDl').onclick=e=>{if($('#gpSvDlB').disabled){e.preventDefault();toast(t('saves.none_yet'),'err');}};
 $('#gpSvImp').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='.zip,application/zip';inp.onchange=async()=>{const f=inp.files[0];if(!f)return;const r=await ask({title:t('saves.import_title'),text:t('saves.import_text',{name:f.name}),ok:t('saves.import_ok'),danger:true});if(!r)return;
   const xh=new XMLHttpRequest();xh.open('PUT','/api/saves/import?'+q);xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast(t('saves.imported',{n:j.written})+(j.prefix_missing?'. '+t('saves.no_prefix'):''),'ok');else toast(j.error||t('saves.import_failed'),'err');loadSaves(g,x);};xh.onerror=()=>toast(t('err.upload'),'err');xh.send(f);toast(t('saves.uploading'));};inp.click();};
 try{const j=await(await fetch('/api/saves/info?'+q)).json();if(j.error){$('#gpSaves').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpSvDlB').disabled=true;return;}
  $('#gpSaves').innerHTML=(j.sources.length?`${t('saves.what',{size:fmt(j.total)})}<ul class="list">${j.sources.map(s=>`<li><code>${esc(s.path)}</code> · ${t('common.files_n',{n:s.files})} · ${fmt(s.size)}</li>`).join('')}</ul>`:t('saves.none'))+(j.prefix?t('saves.prefix_html',{path:esc(j.prefix)}):'');
  $('#gpSvDlB').disabled=!j.total;}catch(e){$('#gpSaves').textContent=t('status.error');}}
// ---- import one game that already lives elsewhere on the Deck
function importNote(j){const a=(j.adopted||[])[0];if(!a)return t('import.added',{name:j.name,n:j.exes.length});
 const bits=[];if(a.name)bits.push(t('import.bit_name',{name:a.name}));if(a.compat)bits.push(compatLabel(a.compat));if(a.covers&&a.covers.length)bits.push(t('import.bit_covers',{n:a.covers.length}));
 return t('import.adopted',{name:j.name,what:bits.join(', ')||t('import.bit_shortcut')});}
$('#importGame').onclick=async()=>{
 openModal(`<h3>${t('import.title')}</h3><p>${t('import.text_html')}</p>
  <label>${t('import.path')}</label><input id="ipath" type="text" placeholder="/home/deck/Games/MyGame/Game.exe" autocomplete="off" spellcheck="false">
  <div class="hint" style="margin-top:6px">${t('import.path_hint')}</div>
  <div id="icand"></div><div class="btns"><button class="ghost" id="icancel">${t('btn.cancel')}</button><button id="iok">${t('btn.add')}</button></div>`);
 $('#icancel').onclick=closeModal;
 const go=async()=>{const v=$('#ipath').value.trim();if(!v){toast(t('import.need_path'),'err');return;}const j=await api('/api/game/import',{path:v});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}};
 $('#iok').onclick=go;$('#ipath').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
 try{const r=await(await fetch('/api/game/import/scan')).json();const c=r.candidates||[];
  if(c.length)$('#icand').innerHTML=`<div class="sect">${t('import.in_steam_only')}</div>`
   +c.map(x=>`<div class="vn" style="cursor:default"><div style="flex:1;min-width:0"><b>${esc(x.name||x.dir.split('/').pop())}</b><small>${esc(x.dir)}${x.exists?'':' · '+t('import.file_missing')}</small></div><button class="ghost sm" data-imppick="${esc(x.dir)}" ${x.exists?'':'disabled'}>${t('import.add_one')}</button></div>`).join('');
 }catch(e){}};
// ---- add to Steam: pick a name (when there are several candidates) and Proton for this game
async function addFlow(game,exe){const g=((lastState&&lastState.games)||[]).find(x=>x.path===game);const x=g&&g.exes.find(e=>e.exe===exe);const own=(x&&(x.clean_name||x.name))||'';
 const names=[own,...((g&&g.names)||[])].filter((n,i,a)=>n&&a.findIndex(m=>m.toLowerCase()===n.toLowerCase())===i);let name=own,tool=null;
 if(names.length>1||!own){const st=(lastState&&lastState.settings)||{};const linux=!!(x&&x.linux);
  const b=openModal(`<h3>${t('addsteam.title')}</h3><p>${t('addsteam.text')}</p><label>${t('common.name')}</label><input id="anm" type="text" value="${esc(own)}" autocomplete="off"><div class="chipsel">${names.map(n=>`<button type="button" data-pick="${esc(n)}">${esc(n)}</button>`).join('')}</div>`
   +(linux?`<div class="hint">${t('addsteam.linux')}</div>`:`<label>${t('addsteam.proton')}</label><select id="atool">${compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`)
   +`<div class="btns"><button class="ghost" id="acancel">${t('btn.cancel')}</button><button id="aok">${t('btn.add')}</button></div>`);
  b.querySelectorAll('[data-pick]').forEach(c=>c.onclick=()=>{$('#anm').value=c.dataset.pick;$('#anm').focus();});
  const res=await new Promise(r=>{$('#acancel').onclick=()=>{closeModal();r(null);};$('#aok').onclick=()=>{const v={name:$('#anm').value.trim(),tool:linux?'':$('#atool').value};closeModal();r(v);};
   $('#anm').addEventListener('keydown',e=>{if(e.key==='Enter')$('#aok').click();});$('#modal').onclick=e=>{if(e.target===$('#modal')){closeModal();r(null);}};});
  if(!res)return;name=res.name||own;tool=res.tool;}
 const k=game+'|'+exe;busy.add(k);render(lastState);const body={game,exe,name};if(tool!==null)body.tool=tool;const j=await api('/api/add_to_steam',body);busy.delete(k);if(j.ok){toast(j.note,'ok');if(view.kind==='game'){extrasFor=null;sigGame='';}}}
// ---- VNDB picker: a whole cover set for the game, or one picked image for one slot
async function vndbPicker(game,exe,name,after,slot){
 openModal(`<h3>${slot?t('vndb.slot_title',{slot:LAB[slot][0]}):t('vndb.title')}</h3><p>${slot?t('vndb.slot_text'):t('vndb.text')} ${t('vndb.experimental')}</p><div class="row"><input id="vq" type="text" value="${esc(name)}"><button id="vgo" class="sm">${t('vndb.search')}</button></div><div id="vres"></div><div class="btns"><button class="ghost" id="vcancel">${t('btn.close')}</button></div>`);
 $('#vcancel').onclick=closeModal;
 const applyAll=async(vn)=>{closeModal();const k='art'+game+'|'+exe;busy.add(k);render(lastState);await api('/api/art',{game,exe,source:'vndb',vn});toast(t('vndb.applying'),'ok');setTimeout(()=>{busy.delete(k);refresh();if(after)after();},8000);};
 const showImages=async(vn,vtitle)=>{$('#vres').innerHTML=`<div class="empty">${t('vndb.loading_images')}</div>`;
  try{const r=await(await fetch('/api/vndb/images?vn='+encodeURIComponent(vn))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
   $('#vres').innerHTML=`<div class="hint" style="margin:10px 0 4px">${esc(vtitle)} · ${t('vndb.tap_image',{slot:LAB[slot][0]})}</div><div class="vimgs">${(r.images||[]).map(im=>`<div class="vimg" data-url="${esc(im.url)}"><img src="${esc(im.url)}" loading="lazy" alt=""><small>${im.kind==='cover'?t('vndb.cover'):t('vndb.screenshot')}${im.dims?' · '+im.dims.join('×'):''}</small></div>`).join('')||`<div class="empty">${t('vndb.no_images')}</div>`}</div><div class="btns"><button class="ghost sm" id="vback">${t('vndb.other_vn')}</button><button class="ghost sm" id="vall">${t('vndb.all_slots')}</button></div>`;
   $('#vback').onclick=search;$('#vall').onclick=()=>applyAll(vn);
   $('#vres').querySelectorAll('.vimg').forEach(el=>el.onclick=async()=>{closeModal();toast(t('vndb.applying_image'));const j=await api('/api/art/from_url',{game,exe,slot,url:el.dataset.url,vn});if(j.ok){toast(t('covers.replaced'),'ok');if(after)after();}});
  }catch(e){$('#vres').innerHTML=`<div class="empty err">${t('err.network')}</div>`;}};
 const search=async()=>{$('#vres').innerHTML=`<div class="empty">${t('vndb.searching')}</div>`;try{const r=await(await fetch('/api/vndb/search?q='+encodeURIComponent($('#vq').value))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
  $('#vres').innerHTML=(r.results||[]).map(v=>`<div class="vn" data-vn="${esc(v.id)}" data-title="${esc(v.title||'')}">${v.image?`<img src="${esc(v.image)}" loading="lazy" alt="">`:`<div class="ph">${t('common.none')}</div>`}<div><b>${esc(v.title)}</b><small>${esc(v.alttitle||'')}${v.released?' · '+esc(v.released):''} · ${esc(v.id)}</small></div></div>`).join('')||`<div class="empty">${t('vndb.nothing')}</div>`;
  $('#vres').querySelectorAll('.vn').forEach(el=>el.onclick=()=>slot?showImages(el.dataset.vn,el.dataset.title):applyAll(el.dataset.vn));}catch(e){$('#vres').innerHTML=`<div class="empty err">${t('err.network')}</div>`;}};
 $('#vgo').onclick=search;$('#vq').addEventListener('keydown',e=>{if(e.key==='Enter')search();});search();}
// ---- archives tab
async function loadArchives(){try{const j=await(await fetch('/api/archives')).json();const a=j.archives||[];$('#archTotal').textContent=a.length?`${a.length} · ${fmt(a.reduce((s,x)=>s+x.size,0))}`:t('common.empty');
 $('#archives').innerHTML=a.map(x=>`<div class="arch"><div class="nm"><b>${esc(x.name)}</b><div class="path">${esc(x.disk)} · ${fmt(x.size)} · ${when(x.time)}</div></div><span class="acts">${x.extracted?`<span class="pill">${t('arch.unpacked')}</span>`:x.archive?`<button class="ghost sm" data-aextract="${esc(x.path)}">${t('jobs.unpack')}</button>`:x.part?`<span class="pill k">${t('arch.part')}</span>`:`<span class="pill k">${t('arch.file')}</span>`}<button class="danger sm" data-adel="${esc(x.path)}" data-name="${esc(x.name)}">${t('arch.delete')}</button></span></div>`).join('')||`<div class="empty">${t('arch.empty')}</div>`;}catch(e){}}
$('#archCleanup').onclick=async()=>{const r=await ask({title:t('arch.cleanup_title'),text:t('arch.cleanup_text'),ok:t('delete.ok'),danger:true});if(!r)return;const j=await api('/api/archive/cleanup',{});if(j.ok){toast(j.removed.length?t('toast.deleted',{what:j.removed.join(', ')}):t('arch.nothing_to_delete'),'ok');loadArchives();}};
$('#inboxClear').onclick=async()=>{let st={};try{st=await(await fetch('/api/inbox/stats')).json();}catch(e){}
 if(!st.removed){toast(st.skipped?t('inbox.only_active'):t('inbox.already_empty'),'ok');return;}
 const r=await ask({title:t('inbox.clear_title'),text:t('inbox.clear_text',{n:st.removed,size:fmt(st.freed)})+(st.skipped?' '+t('inbox.clear_keeps',{n:st.skipped}):''),ok:t('inbox.clear_ok'),danger:true});if(!r)return;
 const j=await api('/api/inbox/clear',{});if(j.ok){toast(t('inbox.cleared',{n:j.removed,size:fmt(j.freed)}),'ok');loadArchives();}};
// ---- settings tab (each control saves on change)
function fillSettings(){const s=lastState;if(!s)return;const st=s.settings||{},c=s.cdp||{};
 $('#sCdpTxt').textContent=c.available?t('cdp.long.live'):c.marker&&c.enabled?t('cdp.long.after_reboot'):c.enabled?t('cdp.long.no_steam'):t('cdp.long.off');
 $('#sCompat').innerHTML=compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('');
 $('#sLinux').checked=!!st.prefer_linux;$('#sCef').checked=!!st.cef_enabled;$('#sVndb').checked=!!st.vndb_auto;$('#sSafe').checked=!st.vndb_nsfw;
 if(document.activeElement!==$('#sPw'))$('#sPw').value=(st.archive_passwords||[]).join(', ');if(document.activeElement!==$('#sUpd'))$('#sUpd').value=st.update_url||'';
 $('#sProxyView').textContent=st.proxy_masked||t('proxy.not_set');$('#sProxyDl').checked=!!st.proxy_downloads;$('#sMega').checked=!!st.mega_verify;
 $('#sInfo').innerHTML=`DeckDrop v${esc(s.version)} · ${s.ffmpeg?t('ffmpeg.found'):t('ffmpeg.missing')}<br>${(s.urls||[]).map(esc).join(' · ')}<br>`+(s.disks||[]).map(d=>`${esc(d.label)}: ${t('disk.free_of',{free:fmt(d.free),total:fmt(d.total)})} · <code>${esc(d.root)}</code>`).join('<br>')+(s.pending?'<br>'+t('info.pending_n',{n:s.pending}):'');fillLang();}
document.querySelectorAll('#pgSettings [data-set]').forEach(el=>el.addEventListener('change',async()=>{const k=el.dataset.set;let v=el.type==='checkbox'?el.checked:el.value;if(el.dataset.invert)v=!v;
 if(k==='archive_passwords')v=v.split(',').map(x=>x.trim()).filter(Boolean);const j=await api('/api/settings',{[k]:v});if(j.ok){toast(t('toast.saved'),'ok');lastState.settings=j.settings;lastState.cdp=j.cdp;fillSettings();}}));
$('#sProxyEdit').onclick=async()=>{const p=await ask({title:t('proxy.title'),text:t('proxy.pin_text'),fields:[{label:'PIN',type:'password',numeric:true}],ok:t('proxy.show')});if(!p)return;
 const rv=await api('/api/settings/reveal',{pin:p[0]});if(!rv.ok)return;
 const r=await ask({title:t('set.net.proxy'),text:t('proxy.edit_text'),fields:[{label:t('proxy.address'),value:rv.proxy||'',placeholder:'socks5://192.168.1.10:10808'}],ok:t('btn.save')});if(!r)return;
 const j=await api('/api/settings',{proxy:r[0],pin:p[0]});if(j.ok){toast(r[0]?t('proxy.saved'):t('proxy.off'),'ok');lastState.settings=j.settings;fillSettings();}};
$('#sTest').onclick=async()=>{const b=$('#sTest');b.disabled=true;b.textContent=t('common.checking');$('#sTestRes').textContent='';
 try{const j=await(await fetch('/api/vndb/test')).json();
  $('#sTestRes').innerHTML=(j.proxy?t('proxy.line_html',{proxy:esc(j.proxy)})+'<br>':t('proxy.none')+'<br>')
   +j.checks.map(c=>`${c.ok?'✅':'❌'} ${esc(c.host)} ${esc(c.mode)}: ${c.ok?(t('unit.ms',{n:c.ms})+(c.note?' · '+esc(c.note):'')):esc(c.error)}`).join('<br>');
  }catch(e){$('#sTestRes').textContent=t('proxy.test_failed');}
 b.disabled=false;b.textContent=t('set.net.test');};
$('#sPinGo').onclick=async()=>{const j=await api('/api/settings/pin',{old:$('#sPinOld').value,new:$('#sPinNew').value});if(j.ok){toast(t('pin.changed'),'ok');$('#sPinOld').value=$('#sPinNew').value='';}};
// ---- performance: the self-check and the measurements run on the Deck, the page only shows them
let perfReport='';
const PERF_ICON={ok:'✅',warn:'⚠️',fail:'❌',info:'ℹ️'};
function duration(s){return s<3600?t('unit.duration_min',{m:Math.floor(s/60)}):t('unit.duration',{h:Math.floor(s/3600),m:Math.floor(s%3600/60)});}
function perfVal(r){const v=r.value;if(v==null)return '—';
 return r.unit==='bytes'?fmt(v):r.unit==='mbps'?t('unit.mb_s',{n:v}):r.unit==='ms'?t('unit.ms',{n:v}):r.unit==='watts'?t('unit.watts',{n:v})
  :r.unit==='pct'?v+'%':r.unit==='duration'?duration(v):String(v);}
async function perfRun(kind){const b=kind==='check'?$('#perfCheck'):$('#perfBench'),label=b.textContent;
 $('#perfCheck').disabled=$('#perfBench').disabled=true;b.textContent=kind==='check'?t('common.checking'):t('perf.measuring');
 const j=await api('/api/perf/'+kind,{});$('#perfCheck').disabled=$('#perfBench').disabled=false;b.textContent=label;if(!j.ok)return;
 const head=`DeckDrop v${(lastState&&lastState.version)||''} · ${label}`;let html,text;
 if(kind==='check'){const c=j.counts,sum=c.fail?t('perf.summary.fail',{n:c.fail}):c.warn?t('perf.summary.warn',{n:c.warn}):t('perf.summary.ok');
  html=`<p class="perf-sum ${c.fail?'fail':c.warn?'warn':'ok'}">${esc(sum)}</p>`+j.items.map(i=>`<div class="perf-item"><span>${PERF_ICON[i.status]}</span><div><b>${esc(i.title)}</b>${i.detail?`<small>${esc(i.detail)}</small>`:''}</div></div>`).join('');
  text=`${head}\n${sum}\n\n`+j.items.map(i=>`${PERF_ICON[i.status]} ${i.title}${i.detail?' — '+i.detail:''}`).join('\n');}
 else{html=j.sections.map(s=>`<h3>${esc(s.title)}</h3><table class="perf-tab">`+s.rows.map(r=>`<tr><td>${esc(r.label)}</td><td><b>${esc(perfVal(r))}</b>${r.note?`<small>${esc(r.note)}</small>`:''}</td></tr>`).join('')+'</table>').join('')
   +`<div class="hint">${esc(t('perf.took',{n:j.seconds}))}</div>`;
  text=head+'\n'+j.sections.map(s=>`\n${s.title}\n`+s.rows.map(r=>`  ${r.label}: ${perfVal(r)}${r.note?' ('+r.note+')':''}`).join('\n')).join('\n');}
 $('#perfRes').innerHTML=html;perfReport=text;$('#perfCopy').hidden=false;
 const dl=$('#perfDl');dl.href='/api/perf/report?kind='+kind;dl.hidden=false;}
$('#perfCheck').onclick=()=>perfRun('check');$('#perfBench').onclick=()=>perfRun('bench');
// the page is plain http on the home network, where browsers keep navigator.clipboard away: select the text instead
$('#perfCopy').onclick=()=>{const ok=()=>toast(t('perf.copied'),'ok');
 const manual=()=>{openModal(`<h3>${esc(t('set.perf.copy'))}</h3><p>${esc(t('perf.copy_hint'))}</p><textarea id="perfTxt" readonly rows="12" style="width:100%">${esc(perfReport)}</textarea><div class="btns"><button id="perfClose">${esc(t('btn.close'))}</button></div>`);
  const ta=$('#perfTxt');ta.focus();ta.select();try{if(document.execCommand('copy'))ok();}catch(e){}$('#perfClose').onclick=closeModal;};
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(perfReport).then(ok,manual);else manual();};
// ---- media
async function mediaEnter(){if(mediaToken){loadMedia();return;}const st=await(await fetch('/api/media/status')).json();const a=$('#mediaAuth');$('#mediaBody').hidden=true;
 if(!st.set){a.innerHTML=`<div class="card auth"><div class="lock">🔐</div><h3 style="margin:0">${t('media.first_title')}</h3><p>${t('media.first_text')}</p>
  <div class="row"><input id="pw1" type="password" placeholder="${t('media.pw_new')}"></div><div class="row"><input id="pw2" type="password" placeholder="${t('media.pw_again')}"><button id="pwset">${t('btn.save')}</button></div></div>`;
  $('#pwset').onclick=async()=>{const p1=$('#pw1').value,p2=$('#pw2').value;if(p1!==p2){toast(t('media.pw_mismatch'),'err');return;}const r=await fetch('/api/media/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p1})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pw2').addEventListener('keydown',e=>{if(e.key==='Enter')$('#pwset').click();});}
 else{a.innerHTML=`<div class="card auth"><div class="lock">🔒</div><h3 style="margin:0">${t('media.title')}</h3><p>${t('media.text')}</p><div class="row"><input id="pw" type="password" placeholder="${t('media.pw')}"><button id="pwgo">${t('media.login')}</button></div></div>`;
  const go=async()=>{const r=await fetch('/api/media/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pwgo').onclick=go;$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')go();});setTimeout(()=>$('#pw').focus(),60);}}
async function mediaOpen(){$('#mediaAuth').innerHTML='';$('#mediaBody').hidden=false;await loadMedia();}
async function loadMedia(rescan){const r=await fetch('/api/media/list'+(rescan?'?refresh=1':''),{headers:{'X-Media-Token':mediaToken}});if(r.status===401){mediaToken=null;mediaEnter();return;}const j=await r.json();mediaItems=j.items;
 $('#mhint').textContent=t('common.files_n',{n:mediaItems.length})+(j.ffmpeg?'':' · '+t('media.no_ffmpeg'));renderMedia();}
function renderMedia(){const q=$('#mq').value.trim().toLowerCase();const list=mediaItems.filter(i=>(mediaFilter==='all'||(mediaFilter==='image'?i.kind==='image':i.kind!=='image'))&&(!q||(i.game+' '+i.name).toLowerCase().includes(q)));
 $('#mgrid').innerHTML=list.slice(0,400).map(i=>`<div class="tile" data-id="${i.id}"><img loading="lazy" src="/media/${i.id}/thumb?t=${mediaToken}" alt=""><span class="k${i.kind==='image'?'':' v'}">${i.kind==='image'?t('media.kind.image'):i.kind==='clip'?t('media.kind.clip'):t('media.kind.video')}</span><div class="cap"><b>${esc(i.game)}</b><span class="muted">${when(i.time)} · ${fmt(i.size)}</span></div></div>`).join('')||`<div class="empty">${t('media.nothing')}</div>`;
 if(list.length>400)$('#mhint').textContent+=' · '+t('media.first_400');}
$('#mq').oninput=renderMedia;$('#mrefresh').onclick=()=>loadMedia(true);
document.querySelectorAll('.chips button[data-f]').forEach(b=>b.onclick=()=>{mediaFilter=b.dataset.f;document.querySelectorAll('.chips button[data-f]').forEach(x=>x.classList.toggle('on',x===b));renderMedia();});
$('#mgrid').addEventListener('click',e=>{const tile=e.target.closest('.tile');if(!tile)return;const i=mediaItems.find(x=>x.id===tile.dataset.id);if(!i)return;viewing=i;
 const src=`/media/${i.id}?t=${mediaToken}`;$('#vtitle').textContent=i.game+' · '+i.name;$('#vopen').href=src;$('#vdl').href=src+'&dl=1';$('#vdl').setAttribute('download',i.name);
 $('#vbody').innerHTML=i.kind==='image'?`<img src="${src}">`:`<video src="${src}" controls playsinline autoplay></video>`;$('#viewer').classList.add('on');});
function closeViewer(){$('#viewer').classList.remove('on');$('#vbody').innerHTML='';viewing=null;}
$('#vclose').onclick=closeViewer;$('#viewer').addEventListener('click',e=>{if(e.target.id==='viewer'||e.target.id==='vbody')closeViewer();});
$('#vdel').onclick=async()=>{if(!viewing)return;const i=viewing;const r=await ask({title:t('delete.title'),text:t('media.delete_text',{name:`${i.game} · ${i.name}`,size:fmt(i.size)})+(i.kind==='clip'?' '+t('media.delete_clip'):''),fields:[{label:'PIN',type:'password',numeric:true}],ok:t('delete.ok'),danger:true});if(!r)return;
 const j=await api('/api/media/delete',{id:i.id,pin:r[0],token:mediaToken});if(j.ok){toast(t('toast.deleted',{what:j.removed}),'ok');closeViewer();loadMedia(true);}};
$('#mediaReset').onclick=async()=>{const r=await ask({title:t('media.reset_title'),text:t('media.reset_text'),fields:[{label:'PIN',type:'password',numeric:true}],ok:t('media.reset_ok'),danger:true});if(!r)return;
 const j=await api('/api/media/reset',{pin:r[0]});if(j.ok){mediaToken=null;toast(t('media.reset_done'),'ok');if(tab==='media')mediaEnter();}};
// ---- interface language: kept per device in a cookie the server reads (none = the Steam language)
function langChoice(){const m=document.cookie.match(/(?:^|;\s*)deckdrop_lang=(\w+)/);return m&&LANGS[m[1]]?m[1]:'';}
function fillLang(){const sel=$('#sLang');if(sel.options.length)return;const cur=langChoice();
 sel.innerHTML=[['',t('set.lang.auto')],...Object.entries(LANGS)].map(([v,n])=>`<option value="${v}" ${v===cur?'selected':''}>${esc(n)}</option>`).join('');}
$('#sLang').onchange=()=>{const v=$('#sLang').value;document.cookie='deckdrop_lang='+(v?v+';max-age=157680000':';max-age=0')+';path=/;SameSite=Lax';location.reload();};
// ---- self update
$('#update').onclick=async()=>{const r=await ask({title:t('footer.update'),text:t('update.text'),fields:[{label:t('update.url'),value:(lastState&&lastState.settings&&lastState.settings.update_url)||''},{label:'PIN',type:'password',numeric:true}],ok:t('update.ok')});if(!r)return;
 const b=$('#update');b.disabled=true;b.textContent=t('update.updating');const old=lastState&&lastState.version;
 const j=await api('/api/update',{url:r[0],pin:r[1]});
 if(!j.ok){b.disabled=false;b.textContent=t('footer.update');return;}
 toast(j.note,'ok');b.textContent=j.note;if(!(j.updated!==undefined?j.updated:/->/.test(j.note))){setTimeout(()=>{b.disabled=false;b.textContent=t('footer.update');},3000);return;}
 const np=j.new_port&&String(j.new_port)!==(location.port||'80')?j.new_port:null;const target=np?`${location.protocol}//${location.hostname}:${np}/`:null;
 if(target){toast(t('update.new_address',{url:target}),'ok');setTimeout(()=>location.href=target,4000);return;}
 let n=0;const timer=setInterval(async()=>{n++;try{const s=await(await fetch('/api/state')).json();if(s.version!==old){clearInterval(timer);b.textContent=t('update.done_button',{v:s.version});toast(t('update.done',{v:s.version}),'ok');setTimeout(()=>location.reload(),1500);}}catch(e){}if(n>40){clearInterval(timer);b.disabled=false;b.textContent=t('footer.update');}},1000);};
