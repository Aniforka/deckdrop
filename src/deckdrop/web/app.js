const $=s=>document.querySelector(s);
const isUrl=t=>/^https?:\/\/\S+$/i.test(t);
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
function when(t){const d=new Date(t*1000);return d.toLocaleDateString('ru-RU')+' '+d.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});}
function toast(msg,kind){const t=document.createElement('div');t.className='toast'+(kind?' '+kind:'');t.textContent=msg;$('#toasts').appendChild(t);setTimeout(()=>{t.style.opacity='0';setTimeout(()=>t.remove(),320);},kind==='err'?5000:3200);}
// ---- modal helpers
function openModal(html){const m=$('#modal'),b=$('#mbox');b.innerHTML=html;m.classList.add('on');m.onclick=e=>{if(e.target===m)closeModal();};const f=b.querySelector('input,select,textarea');if(f)setTimeout(()=>f.focus(),60);return b;}
function closeModal(){$('#modal').classList.remove('on');$('#mbox').innerHTML='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeViewer();}});
function ask(o){return new Promise(res=>{const b=openModal(`<h3>${esc(o.title)}</h3>${o.text?`<p>${esc(o.text)}</p>`:''}${o.html||''}`
  +(o.fields||[]).map((f,i)=>f.type==='check'?`<label class="chk"><input id="mf${i}" type="checkbox" ${f.value?'checked':''}>${esc(f.label)}</label>`
   :f.type==='select'?`<label>${esc(f.label)}</label><select id="mf${i}">${f.options.map(op=>`<option value="${esc(op.value)}" ${op.value===f.value?'selected':''}>${esc(op.label)}</option>`).join('')}</select>`
   :`<label>${esc(f.label)}</label><input id="mf${i}" type="${f.type||'text'}" value="${esc(f.value||'')}" placeholder="${esc(f.placeholder||'')}" autocomplete="off" ${f.numeric?'inputmode="numeric"':''}>`).join('')
  +`<div class="btns"><button class="ghost" id="mcancel">Отмена</button><button id="mok" class="${o.danger?'danger':''}">${esc(o.ok||'ОК')}</button></div>`);
 const done=v=>{closeModal();res(v);};
 $('#mcancel').onclick=()=>done(null);$('#mok').onclick=()=>done((o.fields||[]).map((f,i)=>{const el=$('#mf'+i);return f.type==='check'?el.checked:el.value;}));
 b.querySelectorAll('input').forEach(inp=>inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&inp.type!=='checkbox')$('#mok').click();}));
 $('#modal').onclick=e=>{if(e.target===$('#modal'))done(null);};});}
async function api(path,body,method){const r=await fetch(path,{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j={};try{j=await r.json();}catch(e){}if(!r.ok)toast(j.error||('ошибка '+r.status),'err');refresh();return j;}
// ---- tabs
function showTab(t){tab=t;$('#pgGames').hidden=t!=='games';$('#pgArch').hidden=t!=='arch';$('#pgMedia').hidden=t!=='media';$('#pgSettings').hidden=t!=='settings';
 [['tabGames','games'],['tabArch','arch'],['tabMedia','media'],['tabSettings','settings']].forEach(([id,k])=>$('#'+id).classList.toggle('on',t===k));
 hdr.classList.remove('hide');window.scrollTo(0,0);
 if(t==='media')mediaEnter();if(t==='arch')loadArchives();if(t==='settings')fillSettings();clearInterval(archTimer);if(t==='arch')archTimer=setInterval(loadArchives,5000);}
$('#tabGames').onclick=()=>showTab('games');$('#tabArch').onclick=()=>showTab('arch');$('#tabMedia').onclick=()=>showTab('media');$('#tabSettings').onclick=()=>showTab('settings');$('#settings').onclick=()=>showTab('settings');
// header slides away when scrolling down on a phone and comes back on scroll up
const hdr=document.querySelector('header');let lastY=0;const mobile=matchMedia('(max-width:560px)');
addEventListener('scroll',()=>{const y=scrollY;if(!mobile.matches){hdr.classList.remove('hide');lastY=y;return;}
 if(y>lastY+8&&y>90)hdr.classList.add('hide');else if(y<lastY-8||y<40)hdr.classList.remove('hide');lastY=y;},{passive:true});
$('#addr').onclick=()=>{const u=lastState&&lastState.urls[0];if(!u)return;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(u).then(()=>toast('Адрес скопирован','ok'),()=>toast(u));else toast(u);};
// ---- downloads / uploads
const diskSel=()=>$('#disk').value||undefined;
function download(u){u=(u||$('#url').value).trim();if(!isUrl(u)){toast('Нужна ссылка вида http(s)://…','err');return;}$('#url').value='';
 if(isMegaFolder(u)){megaPick(u);return;}api('/api/download',{url:u,disk:diskSel()});}
const MEGA_RE=/^https?:\/\/(?:www\.)?mega(?:\.co)?\.nz\//i;
function isMegaFolder(u){if(!MEGA_RE.test(u))return false;const h=u.split('#')[1]||'';return /\/folder\//i.test(u.split('#')[0])||/^F!/.test(h);}
async function megaPick(url){const j=await api('/api/mega/list',{url});const fs=(j&&j.files)||[];
 if(!j.ok||!fs.length){$('#url').value=url;if(j.ok)toast('В этой папке Mega нет файлов','err');return;}
 if(fs.length===1){const r=await api('/api/mega/download',{url,nodes:[fs[0].h],disk:diskSel()});if(r.ok)toast('Скачиваю: '+fs[0].name,'ok');return;}
 const b=openModal(`<h3>Папка на Mega</h3><p>${esc(j.name||'')} · файлов: ${fs.length} · ${fmt(j.total)}</p>`
  +`<div class="row" style="gap:8px"><button class="ghost sm" id="mAll">выбрать все</button><button class="ghost sm" id="mNone">снять все</button></div>`
  +`<div class="mlist">${fs.map((f,i)=>`<label class="vn"><input type="checkbox" data-mi="${i}" checked><div style="flex:1;min-width:0"><b>${esc(f.name)}</b><small>${f.dir?esc(f.dir)+' · ':''}${fmt(f.size)}</small></div></label>`).join('')}</div>`
  +`<div class="hint" id="mHint" style="margin-top:8px"></div><div class="btns"><button class="ghost" id="mCancel">Отмена</button><button id="mOk">Скачать</button></div>`);
 const boxes=[...b.querySelectorAll('[data-mi]')];
 const upd=()=>{const n=boxes.filter(x=>x.checked).length;$('#mOk').textContent=n?`Скачать (${n})`:'Скачать';$('#mOk').disabled=!n;
  $('#mHint').textContent=n>1?`Отмеченное скачается одним заданием в папку «${j.name||'Mega'}» со всей структурой. Если там только архивы, они распакуются.`:n===1?'Один файл попадёт во входящие, как обычная ссылка.':'';};
 boxes.forEach(x=>x.onchange=upd);$('#mAll').onclick=()=>{boxes.forEach(x=>x.checked=true);upd();};$('#mNone').onclick=()=>{boxes.forEach(x=>x.checked=false);upd();};
 $('#mCancel').onclick=closeModal;
 $('#mOk').onclick=async()=>{const nodes=boxes.filter(x=>x.checked).map(x=>fs[+x.dataset.mi].h);closeModal();
  const r=await api('/api/mega/download',{url,nodes,disk:diskSel()});if(r.ok)toast(nodes.length>1?`Скачиваю папку «${j.name||'Mega'}» · файлов: ${nodes.length}`:'Скачиваю: '+((r.jobs[0]||{}).label||''),'ok');};
 upd();}
$('#go').onclick=()=>download();
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')download();});
$('#clear').onclick=()=>api('/api/clear',{});
$('#stopAll').onclick=async()=>{const n=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;
 const r=await ask({title:'Остановить скачивание',text:`Остановить все скачивания (${n})? Задания из очереди исчезнут сразу, текущее остановится, а недокачанное удалится.`,ok:'Остановить',danger:true});if(!r)return;
 const j=await api('/api/cancel_all',{});if(j.ok)toast(j.stopped?`Остановлено: ${j.stopped}`:'Нечего останавливать','ok');};
$('#toggleHidden').onclick=()=>{showHidden=!showHidden;render(lastState);};
$('#disk').onchange=()=>api('/api/settings',{default_disk:$('#disk').value});
function upload(f){const x=new XMLHttpRequest();x.open('PUT','/api/upload/'+encodeURIComponent(f.name)+(diskSel()?'?disk='+encodeURIComponent(diskSel()):''));x.onerror=()=>toast('Ошибка загрузки: '+f.name,'err');x.onload=refresh;x.send(f);setTimeout(refresh,300);}
$('#file').onchange=e=>{[...e.target.files].forEach(upload);e.target.value='';};
const d=$('#drop');
['dragenter','dragover'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.add('over');}));
['dragleave','drop'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.remove('over');}));
d.addEventListener('drop',e=>{if(e.dataTransfer.files.length){[...e.dataTransfer.files].forEach(upload);return;}
 const t=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split('\n')[0].trim();if(isUrl(t))download(t);});
document.addEventListener('paste',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;const t=(e.clipboardData.getData('text')||'').trim();if(isUrl(t))download(t);});
// ---- game actions (event delegation; lists re-render only when their data changes)
document.addEventListener('change',e=>{const el=e.target;if(el.dataset.compat!==undefined)api('/api/game/compat',{game:el.dataset.game,exe:el.dataset.exe,tool:el.value}).then(j=>{if(j.ok)toast(j.note,'ok');});});
document.addEventListener('click',async e=>{const card=e.target.closest('[data-open]');if(card&&!e.target.closest('button,select,input,a')){openGame(card.dataset.open);return;}
 const b=e.target.closest('button');if(!b)return;const ds=b.dataset;
 if(ds.cancel)api('/api/cancel',{id:+ds.cancel});
 if(ds.pwgo!==undefined){const box=b.closest('.item');const pw=box.querySelector('input[type=password]').value;const rem=box.querySelector('input[type=checkbox]').checked;if(!pw){toast('Введи пароль','err');return;}api('/api/job/password',{id:+ds.pwgo,password:pw,remember:rem});}
 if(ds.add!==undefined)addFlow(ds.game,ds.add);
 if(ds.rename!==undefined){const r=await ask({title:'Имя в библиотеке Steam',fields:[{label:'Название',value:ds.name}],ok:'Переименовать'});if(!r)return;const j=await api('/api/game/rename',{game:ds.game,exe:ds.rename,name:r[0]});if(j.ok)toast(j.note,'ok');}
 if(ds.hide!==undefined)api('/api/game/hide',{path:ds.hide,hidden:ds.hidden==='1'});
 if(ds.unimport!==undefined){const r=await ask({title:'Убрать из DeckDrop',text:`«${ds.name}» пропадёт из списка. Файлы на деке и ярлык в Steam останутся на месте.`,ok:'Убрать',danger:true});if(!r)return;
  const j=await api('/api/game/unimport',{path:ds.unimport});if(j.ok){toast('Убрано из списка: '+j.removed,'ok');if(view.kind==='game')closeGame();}}
 if(ds.imppick!==undefined){const j=await api('/api/game/import',{path:ds.imppick});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}}
 if(ds.del!==undefined){const r=await ask({title:'Удалить с дека',text:`«${ds.name}» будет удалена вместе со скачанным архивом.`,fields:[{label:'Убрать ярлык и из Steam',type:'check',value:true},{label:'PIN',type:'password',numeric:true}],ok:'Удалить',danger:true});if(!r)return;
  const j=await api('/api/game/delete',{path:ds.del,pin:r[1],remove_shortcut:r[0]});if(j.ok){toast('Удалено: '+j.removed.join(', ')+(j.notes&&j.notes.length?'. '+j.notes.join('; '):''),'ok');if(view.kind==='game')closeGame();}}
 if(ds.aextract!==undefined)api('/api/archive/extract',{path:ds.aextract}).then(j=>{if(j.id){toast('Распаковываю: '+j.label,'ok');showTab('games');}});
 if(ds.adel!==undefined){const r=await ask({title:'Удалить архив',text:ds.name,ok:'Удалить',danger:true});if(!r)return;const j=await api('/api/archive/delete',{path:ds.adel});if(j.ok){toast('Удалено: '+j.removed.join(', '),'ok');loadArchives();}}
});
const CANCELLABLE=['queued','resolving','downloading'];let lastJobs=[];
const RU={cancelled:'отменено',queued:'в очереди',resolving:'ищу файл',downloading:'скачиваю',uploading:'принимаю',extracting:'распаковываю',needs_password:'нужен пароль',done:'готово',error:'ошибка'};
function compatOptions(cur){const tools=(lastState&&lastState.compat_tools)||[];let opts=tools.map(t=>({value:t.name,label:t.label}));if(cur&&!opts.some(o=>o.value===cur))opts.unshift({value:cur,label:cur});opts.push({value:'',label:'без Proton (нативно)'});return opts;}
function compatLabel(v){const t=((lastState&&lastState.compat_tools)||[]).find(t=>t.name===v);return t?t.label:(v||'без Proton');}
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
 if(busy.has(k))right='<span class="pill warn">добавляю…</span>';
 else if(x.in_steam)right=`<span class="pill">✓ в Steam</span>${x.pending?'<span class="pill warn" title="применится, когда включится управление Steam">в очереди</span>':''}`+(x.linux?'':`<select class="sel" data-compat data-game="${esc(g.path)}" data-exe="${esc(x.exe)}">${compatOptions(x.compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(x.compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`);
 else right=`<button class="sm" data-add="${esc(x.exe)}" data-game="${esc(g.path)}">в Steam</button>`;
 return `<div class="exe"><div class="exeh"><span class="n">${esc(x.exe)}</span><span class="pill k">${x.linux?'Linux':'Windows'}</span>${x.recommended?'<span class="pill acc">рекомендуется</span>':''}<span class="acts">${right}</span></div></div>`;}
function listCard(g){const x=g.exes.find(e=>e.in_steam);
 return `<div class="item gcard${g.hidden?' hid':''}" data-open="${esc(g.path)}"><div class="top"><b>${esc(g.title||g.name)}</b><span class="acts"><span class="pill k">${esc(g.disk)}</span>${g.imported?'<span class="pill acc" title="игра лежит вне папок DeckDrop">своя</span>':''}${x?'<span class="pill">✓ в Steam</span>':'<span class="pill k">не добавлена</span>'}${x&&x.pending?'<span class="pill warn">в очереди</span>':''}${x&&x.art?'<span class="pill">обложки ✓</span>':''}<span class="chev">›</span></span></div><div class="path">${esc(g.path)}</div></div>`;}
function renderList(s){lastJobs=s.jobs||[];
 const running=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;$('#stopAll').hidden=!running;$('#stopAll').textContent=running>1?`остановить всё (${running})`:'остановить всё';
 const jsig=JSON.stringify(s.jobs);
 if(jsig!==sigJobs&&!focusWithin('#jobs')){sigJobs=jsig;
 $('#jobs').innerHTML=s.jobs.map(j=>{const act=j.status==='downloading'||j.status==='uploading';const pct=j.total?Math.round(j.done*100/j.total):0;
  let st=RU[j.status]||j.status;if(act)st+=' · '+fmt(j.done)+(j.total?' / '+fmt(j.total)+' · '+pct+'%':'')+(j.speed?' · '+fmt(j.speed)+'/с':'');
  const cls=j.status==='error'?'err':j.status==='done'?'ok':j.status==='cancelled'?'':'busy';const can=j.kind==='download'&&CANCELLABLE.includes(j.status);
  return `<div class="item"><div class="top"><b>${esc(j.label)}</b><span class="acts"><span class="pill k">${esc(j.disk||'')}</span>${can?`<button class="ghost sm" data-cancel="${j.id}">отмена</button>`:''}</span></div>`
   +(act?`<div class="bar"><i style="width:${pct}%"></i></div>`:'')+`<div class="st ${cls}">${esc(st)}${j.error?' — '+esc(j.error):''}</div>`
   +(j.status==='needs_password'?`<div class="row wrap" style="margin-top:8px"><input type="password" placeholder="пароль архива" style="flex:1;min-width:140px;padding:8px 12px;font-size:.95em"><label class="small muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" checked>запомнить</label><button class="sm" data-pwgo="${j.id}">распаковать</button></div>`:'')
   +(j.game_dir?`<div class="path">→ ${esc(j.game_dir)}</div>`:j.status==='done'&&j.file?`<div class="path">→ ${esc(j.file)}</div>`:'')+'</div>';}).join('')||'<div class="empty">Пока пусто. Кинь ссылку или файл выше.</div>';}
 const gsig=JSON.stringify([s.games,showHidden]);
 if(gsig!==sigGames){sigGames=gsig;const nh=s.games.filter(g=>g.hidden).length;$('#toggleHidden').textContent=showHidden?'убрать скрытые':`показать скрытые (${nh})`;$('#toggleHidden').hidden=!nh&&!showHidden;
  $('#games').innerHTML=s.games.filter(g=>showHidden||!g.hidden).map(listCard).join('')||'<div class="empty">Игр пока нет.</div>';}}
function renderGame(s){const g=(s.games||[]).find(x=>x.path===view.path);
 if(!g){$('#gpHead').innerHTML='<div class="empty">Игра не найдена, возможно уже удалена.</div>';$('#gpExes').innerHTML='';$('#gpCoversCard').hidden=$('#gpSavesCard').hidden=$('#gpFilesCard').hidden=true;$('#gpActs').innerHTML='';$('#gpInfo').innerHTML='';return;}
 if(extrasFor!==g.path){extrasFor=g.path;loadGameExtras(g);}
 const sig=JSON.stringify([g,[...busy],s.compat_tools,gameInfo]);if(sig===sigGame||focusWithin('#gamePage')||Date.now()<holdGames)return;sigGame=sig;
 const ch=chosenExe(g);const inSteam=g.exes.some(x=>x.in_steam);
 $('#gpHead').innerHTML=`<div class="top"><div style="min-width:0"><div class="gtitle">${esc(g.title||g.name)}${inSteam?`<button class="ghost sm" data-rename="${esc(ch.exe)}" data-game="${esc(g.path)}" data-name="${esc(ch.clean_name||ch.name)}" title="переименовать">✎</button>`:''}</div><div class="path">${esc(g.path)}</div></div><span class="acts"><span class="pill k">${esc(g.disk)}</span>${inSteam?'<span class="pill">✓ в Steam</span>':'<span class="pill k">не добавлена</span>'}${g.imported?'<span class="pill acc" title="папка вне DeckDrop, добавлена вручную">своя</span>':''}${g.hidden?'<span class="pill warn">скрыта из списка</span>':''}</span></div>`;
 $('#gpExes').innerHTML=g.exes.map(x=>exeRow(g,x)).join('')||'<div class="empty">исполняемых файлов не найдено</div>';
 $('#gpCoversCard').hidden=!inSteam;$('#gpSavesCard').hidden=!inSteam;$('#gpFilesCard').hidden=!g.exes.length;
 $('#gpActs').innerHTML=(g.hidden?`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="0">вернуть в список</button>`:`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="1">скрыть из списка</button>`)+(g.imported?`<button class="ghost sm" data-unimport="${esc(g.path)}" data-name="${esc(g.title||g.name)}">убрать из DeckDrop</button>`:'')
  +`<button class="danger sm" data-del="${esc(g.path)}" data-name="${esc(g.title||g.name)}">удалить с дека 🔒</button>`
  +(g.imported?'<span class="hint" style="flex-basis:100%">Игра добавлена вручную и лежит вне папок DeckDrop. «Убрать из DeckDrop» только прячет её из списка, файлы остаются на месте.</span>':'');
 const info=[`Папка: <code>${esc(g.name)}</code>`,`Диск: ${esc(g.disk)}`];
 if(gameInfo&&gameInfo.path===g.path)info.push(`Размер: ${fmt(gameInfo.size)} · файлов: ${gameInfo.files}`);
 if(ch&&ch.in_steam){info.push(`Имя в Steam: ${esc(ch.name)}`);if(ch.appid)info.push(`Steam AppID: <code>${ch.appid}</code>`);if(!ch.linux)info.push(`Proton: ${esc(compatLabel(ch.compat))}${ch.compat_from==='steam'?' · из настроек Steam':''}`);
  if(ch.art_source)info.push(`Обложки: ${ch.art_source==='vndb'?'VNDB · '+esc(ch.vndb_title||''):ch.art_source==='custom'?'свои картинки':ch.art_source==='steam'?'уже были в Steam':'из иконки exe'}`);if(ch.art_note)info.push(esc(ch.art_note));if(ch.art_error)info.push(`<span class="err">${esc(ch.art_error)}</span>`);if(ch.pending)info.push('<span class="busy">Имя и Proton применятся, когда включится управление Steam</span>');}
 $('#gpInfo').innerHTML=info.join('<br>');}
function render(s){if(!s)return;
 $('#ver').textContent='v'+s.version;
 const c=s.cdp||{};const cdpTxt=!c.enabled?'управление Steam выключено':c.available?'управление Steam активно':c.marker?'управление Steam включится после перезагрузки дека':'управление Steam: нет папки Steam';
 $('#ffm').textContent=(s.ffmpeg?'ffmpeg найден':'ffmpeg не найден: обложки попроще, VNDB частично, клипы без звука')+' · '+cdpTxt+(s.pending?` · в очереди: ${s.pending}`:'');
 $('#addr').textContent=s.urls[0]||'';$('#addr').title=s.urls.join('\n');
 const dsel=$('#disk');const dsig=JSON.stringify([s.disks,(s.settings||{}).default_disk]);
 if(dsig!==sigDisks&&document.activeElement!==dsel){const cur=dsel.value||(s.settings&&s.settings.default_disk)||'internal';dsel.innerHTML=(s.disks||[]).map(d=>`<option value="${esc(d.id)}" ${d.id===cur?'selected':''}>${esc(d.label)} · ${fmt(d.free)} свободно</option>`).join('');dsel.hidden=(s.disks||[]).length<2;sigDisks=dsig;}
 if(view.kind==='game')renderGame(s);else renderList(s);}
async function refresh(){try{lastState=await(await fetch('/api/state')).json();render(lastState);}catch(e){}}
applyView(parseHash());refresh();setInterval(refresh,1000);
// ---- game page extras: folder size, covers with preview/replace, saves
const LAB={portrait:['Вертикальная','600×900, Big Picture','2/3'],landscape:['Широкая','920×430','920/430'],hero:['Баннер hero','1920×620','1920/620'],logo:['Логотип','прозрачный PNG','16/5'],icon:['Иконка','квадрат','1/1']};
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
async function loadFiles(g){let j;try{j=await(await fetch('/api/game/dir?'+filesQ(g))).json();}catch(e){j={error:'нет связи с деком'};}
 if(j.error){$('#gpFWhere').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpFList').innerHTML='';$('#gpFUp').disabled=$('#gpFArch').disabled=true;return null;}
 $('#gpFUp').disabled=$('#gpFArch').disabled=false;
 $('#gpFWhere').innerHTML=`Куда: <code>${esc(j.game_rel==='.'?'корень папки игры':j.game_rel)}</code>`+(j.exists?'':' · <span class="busy">такой папки ещё нет, будет создана</span>');
 const n=j.entries.length+j.more;
 $('#gpFList').innerHTML=j.exists?(n?`<details class="hint"><summary>Сейчас в этой папке: ${n}</summary><ul class="list">${j.entries.map(e=>`<li>${e.dir?'📁 ':''}<code>${esc(e.name)}</code>${e.dir?'':' · '+fmt(e.size)}</li>`).join('')}${j.more?`<li>и ещё ${j.more}</li>`:''}</ul></details>`:'<div class="hint">папка пустая</div>'):'';
 return j;}
function putGameFile(q,f,replace){return new Promise(res=>{const xh=new XMLHttpRequest();
 xh.open('PUT',`/api/game/file?${q}&name=${encodeURIComponent(f.name)}${replace?'&replace=1':''}`);
 xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=`Загружаю ${f.name}: ${Math.round(e.loaded*100/e.total)}% · ${fmt(e.loaded)} из ${fmt(e.total)}`;};
 xh.onload=async()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}
  if(xh.status===409&&r.exists&&!replace){const ok=await ask({title:'Заменить файл',text:`«${f.name}» уже есть в этой папке. Заменить? Самый первый вариант DeckDrop сохранит рядом как .bak.`,ok:'Заменить',danger:true});return res(ok?await putGameFile(q,f,true):false);}
  if(xh.status===200){toast(`${f.name} → ${r.rel}${r.backup?' · прежний сохранён как '+r.backup:''}`,'ok');res(true);}else{toast(r.error||'ошибка загрузки','err');res(false);}};
 xh.onerror=()=>{toast('ошибка загрузки: '+f.name,'err');res(false);};xh.send(f);});}
async function uploadGameFiles(g,files){if(!files.length)return;const j=await loadFiles(g);if(!j)return;
 const dirs=new Set(j.entries.filter(e=>e.dir).map(e=>e.name)),have=new Set(j.entries.filter(e=>!e.dir).map(e=>e.name));
 const asDir=files.find(f=>dirs.has(f.name));if(asDir){toast(`«${asDir.name}» здесь уже папка, файл с таким именем не положить`,'err');return;}
 const clash=files.filter(f=>have.has(f.name)).map(f=>f.name);
 if(clash.length){const r=await ask({title:'Заменить файлы',text:`Уже есть: ${clash.join(', ')}. Заменить? Самый первый вариант каждого DeckDrop сохранит рядом как .bak.`,ok:'Заменить',danger:true});if(!r)return;}
 const q=filesQ(g);$('#gpFUp').disabled=true;let done=0;
 try{for(const f of files){if(!await putGameFile(q,f,clash.includes(f.name)))break;done++;}}finally{$('#gpFUp').disabled=false;}
 if(done>1)toast(`Загружено файлов: ${done}`,'ok');loadFiles(g);}
// ---- an archive unpacked over the game: upload, unpack aside, preview, then apply
const ARCH_RE=/\.(zip|7z|rar|tar|tgz|txz|tbz2|tar\.(gz|xz|bz2))$/i;
async function unpackArchive(g,f){
 if(!ARCH_RE.test(f.name)){toast('Это не архив: подойдут zip, 7z, rar и tar','err');return;}
 if(!await loadFiles(g))return;
 $('#gpFUp').disabled=$('#gpFArch').disabled=true;
 try{let r=await new Promise(res=>{const xh=new XMLHttpRequest();xh.open('PUT',`/api/game/archive?${filesQ(g)}&name=${encodeURIComponent(f.name)}`);
   xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=`Загружаю ${f.name}: ${Math.round(e.loaded*100/e.total)}%`;};
   xh.upload.onload=()=>{$('#gpFWhere').textContent='Распаковываю на деке и смотрю, что поменяется…';};
   xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}res(xh.status===200?j:{error:j.error||'ошибка '+xh.status});};
   xh.onerror=()=>res({error:'ошибка загрузки: '+f.name});xh.send(f);});
  if(r.error&&!r.needs_password){toast(r.error,'err');return;}
  while(r.needs_password){const p=await ask({title:'Архив с паролем',text:r.error?`«${r.name}»: неверный пароль, попробуй ещё раз.`:`«${r.name}» закрыт паролем.`,fields:[{label:'Пароль',type:'password'},{label:'Запомнить для других архивов',type:'check',value:true}],ok:'Открыть'});
   if(!p||!p[0]){api('/api/game/archive/discard',{token:r.token});return;}
   $('#gpFWhere').textContent='Распаковываю…';r=await api('/api/game/archive/unlock',{token:r.token,password:p[0],remember:p[1]});if(!r.ok)return;}
  await patchPreview(g,r);
 }finally{$('#gpFUp').disabled=$('#gpFArch').disabled=false;loadFiles(g);}}
function patchPreview(g,r){return new Promise(done=>{
 const where=r.target_rel==='.'?'корень папки игры':r.target_rel;
 const stat=v=>{let h=`Файлов: ${v.files} · ${fmt(v.size)}. `+(v.conflicts?`Заменит существующих: ${v.conflicts} (${v.sample.map(esc).join(', ')}${v.conflicts>v.sample.length?', …':''}).`:'Существующие файлы не затронет.');
  if(v.blocked_count)h+=`<br><span class="err">Не ляжет, мешают папки или файлы с тем же именем: ${v.blocked_count} (${v.blocked.map(esc).join(', ')}${v.blocked_count>v.blocked.length?', …':''})</span>`;return h;};
 openModal(`<h3>Распаковать в игру</h3><p>«${esc(r.name)}» → <code>${esc(where)}</code></p>`
  +`<div class="hint">Внутри: ${r.top.map(t=>`<code>${esc(t)}</code>`).join(' ')||'пусто'}${r.top_more?` и ещё ${r.top_more}`:''}</div>`
  +(r.single_top?`<label class="chk"><input type="checkbox" id="pvStrip">Без верхней папки «${esc(r.single_top)}»: её содержимое ляжет прямо в ${esc(where)}</label>`:'')
  +`<div class="hint" id="pvStat"></div>`
  +`<label class="chk"><input type="checkbox" id="pvBak" checked>Сохранить заменяемые файлы как .bak (только самый первый вариант)</label>`
  +`<div class="btns"><button class="ghost" id="pvCancel">Отмена</button><button id="pvOk">Распаковать</button></div>`);
 const cur=()=>$('#pvStrip')&&$('#pvStrip').checked?r.stripped:r.plain;
 const upd=()=>{const v=cur();$('#pvStat').innerHTML=stat(v);$('#pvOk').disabled=!v.files;$('#pvOk').className=v.conflicts?'danger':'';
  $('#pvOk').textContent=!v.files?'Нечего распаковывать':v.conflicts?`Распаковать и заменить (${v.conflicts})`:'Распаковать';};
 if($('#pvStrip'))$('#pvStrip').onchange=upd;upd();
 const cancel=()=>{closeModal();api('/api/game/archive/discard',{token:r.token});done(false);};
 $('#pvCancel').onclick=cancel;$('#modal').onclick=e=>{if(e.target===$('#modal'))cancel();};
 $('#pvOk').onclick=async()=>{const strip=!!($('#pvStrip')&&$('#pvStrip').checked),backup=$('#pvBak').checked;closeModal();
  $('#gpFWhere').textContent='Распаковываю в игру…';
  const a=await api('/api/game/archive/apply',{token:r.token,strip,backup});
  if(a.ok)toast(`Распаковано в ${a.target_rel==='.'?'корень игры':a.target_rel}: файлов ${a.written}${a.replaced?`, заменено ${a.replaced}`:''}${a.backups?`, в .bak: ${a.backups}`:''}${a.skipped_count?`, пропущено ${a.skipped_count}`:''}`,'ok');
  done(!!a.ok);};});}
async function loadCovers(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpCvHint').textContent='смотрю…';
 $('#gpVndb').onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x));
 $('#gpIcon').onclick=async()=>{const j=await api('/api/art',{game:g.path,exe:x.exe,source:'icon'});if(j.ok){toast('Рисую обложки из иконки…','ok');setTimeout(()=>loadCovers(g,x),5000);}};
 try{const j=await(await fetch('/api/art/current?'+q)).json();if(j.error){$('#gpCvHint').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpCovers').innerHTML='';return;}
  $('#gpCvHint').textContent=(j.source==='vndb'?'Источник: VNDB · '+(j.vndb_title||''):j.source==='custom'?'Источник: свои картинки':j.source==='icon'?'Источник: иконка exe':j.source==='steam'?'Обложки уже были в Steam':'Обложек ещё нет')+(j.live?'':' · Steam покажет новые обложки после перезапуска')+(j.note?' · '+j.note:'')+(j.error?' · '+j.error:'')+'. Нажми «заменить…», чтобы поставить свою картинку в слот.';
  $('#gpCovers').innerHTML=Object.keys(LAB).map(sl=>{const it=j.slots[sl];const [t,sz,ar]=LAB[sl];return `<div class="cov"><div class="im" style="--ar:${ar}">${it?`<img src="${esc(it.url)}" alt="">`:'<span class="muted small">нет</span>'}</div><b>${t}</b><small>${sz}${it?' · '+fmt(it.size):''}</small><div class="acts" style="justify-content:center"><button class="ghost sm" data-cup="${sl}">заменить…</button><button class="ghost sm" data-cvn="${sl}">VNDB…</button>${sl==='icon'?`<button class="ghost sm" data-cexe="${sl}">из exe</button>`:''}</div></div>`;}).join('');
  $('#gpCovers').querySelectorAll('[data-cvn]').forEach(bt=>bt.onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x),bt.dataset.cvn));
  $('#gpCovers').querySelectorAll('[data-cexe]').forEach(bt=>bt.onclick=async()=>{bt.disabled=true;bt.textContent='беру…';
   const jj=await api('/api/art/from_exe',{game:g.path,exe:x.exe,slot:bt.dataset.cexe});if(jj.ok)toast(`Иконка взята из ${x.exe} · ${jj.size}`,'ok');setTimeout(()=>loadCovers(g,x),600);});
  $('#gpCovers').querySelectorAll('[data-cup]').forEach(bt=>bt.onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='image/png,image/jpeg';inp.onchange=()=>{const f=inp.files[0];if(!f)return;bt.disabled=true;bt.textContent='загружаю…';
   const xh=new XMLHttpRequest();xh.open('PUT','/api/art/upload?'+q+'&slot='+bt.dataset.cup);xh.onload=()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast('Обложка заменена','ok');else toast(r.error||'ошибка загрузки','err');setTimeout(()=>loadCovers(g,x),600);refresh();};xh.onerror=()=>{toast('ошибка загрузки','err');loadCovers(g,x);};xh.send(f);};inp.click();});
 }catch(e){$('#gpCvHint').textContent='ошибка';}}
async function loadSaves(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpSaves').textContent='смотрю…';$('#gpSvDl').href='/api/saves/backup?'+q;
 $('#gpSvDl').onclick=e=>{if($('#gpSvDlB').disabled){e.preventDefault();toast('Сохранений пока не найдено','err');}};
 $('#gpSvImp').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='.zip,application/zip';inp.onchange=async()=>{const f=inp.files[0];if(!f)return;const r=await ask({title:'Импорт сейвов',text:`Файлы из «${f.name}» заменят текущие сохранения. Перед этим DeckDrop сам сделает резервную копию текущих.`,ok:'Импортировать',danger:true});if(!r)return;
   const xh=new XMLHttpRequest();xh.open('PUT','/api/saves/import?'+q);xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast(`Импортировано файлов: ${j.written}${j.prefix_missing?'. Префикс Proton ещё не создан, запусти игру один раз':''}`,'ok');else toast(j.error||'ошибка импорта','err');loadSaves(g,x);};xh.onerror=()=>toast('ошибка загрузки','err');xh.send(f);toast('Загружаю бэкап…');};inp.click();};
 try{const j=await(await fetch('/api/saves/info?'+q)).json();if(j.error){$('#gpSaves').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpSvDlB').disabled=true;return;}
  $('#gpSaves').innerHTML=(j.sources.length?`Что войдёт в бэкап (${fmt(j.total)}):<ul class="list">${j.sources.map(s=>`<li><code>${esc(s.path)}</code> · ${s.files} файлов · ${fmt(s.size)}</li>`).join('')}</ul>`:'Сохранений не найдено: игра ещё не запускалась или хранит их в необычном месте. Импорт всё равно доступен.')+(j.prefix?`Префикс Proton: <code>${esc(j.prefix)}</code>`:'');
  $('#gpSvDlB').disabled=!j.total;}catch(e){$('#gpSaves').textContent='ошибка';}}
// ---- import one game that already lives elsewhere on the Deck
function importNote(j){const a=(j.adopted||[])[0];if(!a)return `Добавлена: ${j.name} · файлов запуска: ${j.exes.length}`;
 const bits=[];if(a.name)bits.push(`имя «${a.name}»`);if(a.compat)bits.push(compatLabel(a.compat));if(a.covers&&a.covers.length)bits.push(`обложек: ${a.covers.length}`);
 return `Добавлена: ${j.name}. Из Steam подтянул ${bits.join(', ')||'ярлык'}`;}
$('#importGame').onclick=async()=>{
 openModal(`<h3>Своя игра</h3><p>Путь к файлу запуска <b>одной игры</b>. DeckDrop ничего не копирует и не устанавливает: игра остаётся там, где лежит, и просто появляется в списке. Имя, Proton, обложки и сейвы у неё меняются так же, как у остальных.</p>
  <label>Путь к файлу запуска</label><input id="ipath" type="text" placeholder="/home/deck/Games/MyGame/Game.exe" autocomplete="off" spellcheck="false">
  <div class="hint" style="margin-top:6px">Можно указать и папку самой игры. Это одна игра, а не папка со списком игр. Добавлять можно из домашней папки и с подключённых носителей.</div>
  <div id="icand"></div><div class="btns"><button class="ghost" id="icancel">Отмена</button><button id="iok">Добавить</button></div>`);
 $('#icancel').onclick=closeModal;
 const go=async()=>{const v=$('#ipath').value.trim();if(!v){toast('Введи путь','err');return;}const j=await api('/api/game/import',{path:v});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}};
 $('#iok').onclick=go;$('#ipath').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
 try{const r=await(await fetch('/api/game/import/scan')).json();const c=r.candidates||[];
  if(c.length)$('#icand').innerHTML=`<div class="sect">Уже в Steam, но не в DeckDrop</div>`
   +c.map(x=>`<div class="vn" style="cursor:default"><div style="flex:1;min-width:0"><b>${esc(x.name||x.dir.split('/').pop())}</b><small>${esc(x.dir)}${x.exists?'':' · файла нет на месте'}</small></div><button class="ghost sm" data-imppick="${esc(x.dir)}" ${x.exists?'':'disabled'}>добавить</button></div>`).join('');
 }catch(e){}};
// ---- add to Steam: pick a name (when there are several candidates) and Proton for this game
async function addFlow(game,exe){const g=((lastState&&lastState.games)||[]).find(x=>x.path===game);const x=g&&g.exes.find(e=>e.exe===exe);const own=(x&&(x.clean_name||x.name))||'';
 const names=[own,...((g&&g.names)||[])].filter((n,i,a)=>n&&a.findIndex(m=>m.toLowerCase()===n.toLowerCase())===i);let name=own,tool=null;
 if(names.length>1||!own){const st=(lastState&&lastState.settings)||{};const linux=!!(x&&x.linux);
  const b=openModal(`<h3>Добавить в Steam</h3><p>Имя в библиотеке. Варианты: из имени файла, других файлов игры и папки.</p><label>Название</label><input id="anm" type="text" value="${esc(own)}" autocomplete="off"><div class="chipsel">${names.map(n=>`<button type="button" data-pick="${esc(n)}">${esc(n)}</button>`).join('')}</div>`
   +(linux?'<div class="hint">Linux-сборка: пойдёт нативно, без Proton.</div>':`<label>Proton для этой игры</label><select id="atool">${compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`)
   +`<div class="btns"><button class="ghost" id="acancel">Отмена</button><button id="aok">Добавить</button></div>`);
  b.querySelectorAll('[data-pick]').forEach(c=>c.onclick=()=>{$('#anm').value=c.dataset.pick;$('#anm').focus();});
  const res=await new Promise(r=>{$('#acancel').onclick=()=>{closeModal();r(null);};$('#aok').onclick=()=>{const v={name:$('#anm').value.trim(),tool:linux?'':$('#atool').value};closeModal();r(v);};
   $('#anm').addEventListener('keydown',e=>{if(e.key==='Enter')$('#aok').click();});$('#modal').onclick=e=>{if(e.target===$('#modal')){closeModal();r(null);}};});
  if(!res)return;name=res.name||own;tool=res.tool;}
 const k=game+'|'+exe;busy.add(k);render(lastState);const body={game,exe,name};if(tool!==null)body.tool=tool;const j=await api('/api/add_to_steam',body);busy.delete(k);if(j.ok){toast(j.note,'ok');if(view.kind==='game'){extrasFor=null;sigGame='';}}}
// ---- VNDB picker: a whole cover set for the game, or one picked image for one slot
async function vndbPicker(game,exe,name,after,slot){
 openModal(`<h3>${slot?`VNDB: картинка для слота «${LAB[slot][0]}»`:'Обложки с VNDB'}</h3><p>${slot?'Найди новеллу, затем выбери обложку или любой скриншот.':'Найди новеллу и выбери её. Обложка встанет в вертикальный слот, скриншот в широкий и hero, логотип нарисуется текстом, иконка возьмётся из exe.'} Экспериментально.</p><div class="row"><input id="vq" type="text" value="${esc(name)}"><button id="vgo" class="sm">Искать</button></div><div id="vres"></div><div class="btns"><button class="ghost" id="vcancel">Закрыть</button></div>`);
 $('#vcancel').onclick=closeModal;
 const applyAll=async(vn)=>{closeModal();const k='art'+game+'|'+exe;busy.add(k);render(lastState);await api('/api/art',{game,exe,source:'vndb',vn});toast('Ставлю обложки с VNDB…','ok');setTimeout(()=>{busy.delete(k);refresh();if(after)after();},8000);};
 const showImages=async(vn,vtitle)=>{$('#vres').innerHTML='<div class="empty">загружаю картинки…</div>';
  try{const r=await(await fetch('/api/vndb/images?vn='+encodeURIComponent(vn))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
   $('#vres').innerHTML=`<div class="hint" style="margin:10px 0 4px">${esc(vtitle)} · нажми картинку, она встанет в слот «${LAB[slot][0]}»</div><div class="vimgs">${(r.images||[]).map(im=>`<div class="vimg" data-url="${esc(im.url)}"><img src="${esc(im.url)}" loading="lazy" alt=""><small>${im.kind==='cover'?'обложка':'скриншот'}${im.dims?' · '+im.dims.join('×'):''}</small></div>`).join('')||'<div class="empty">картинок нет</div>'}</div><div class="btns"><button class="ghost sm" id="vback">← другая новелла</button><button class="ghost sm" id="vall">все слоты с этой новеллы</button></div>`;
   $('#vback').onclick=search;$('#vall').onclick=()=>applyAll(vn);
   $('#vres').querySelectorAll('.vimg').forEach(el=>el.onclick=async()=>{closeModal();toast('Ставлю картинку…');const j=await api('/api/art/from_url',{game,exe,slot,url:el.dataset.url,vn});if(j.ok){toast('Обложка заменена','ok');if(after)after();}});
  }catch(e){$('#vres').innerHTML='<div class="empty err">ошибка сети</div>';}};
 const search=async()=>{$('#vres').innerHTML='<div class="empty">ищу…</div>';try{const r=await(await fetch('/api/vndb/search?q='+encodeURIComponent($('#vq').value))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
  $('#vres').innerHTML=(r.results||[]).map(v=>`<div class="vn" data-vn="${esc(v.id)}" data-title="${esc(v.title||'')}">${v.image?`<img src="${esc(v.image)}" loading="lazy" alt="">`:'<div class="ph">нет</div>'}<div><b>${esc(v.title)}</b><small>${esc(v.alttitle||'')}${v.released?' · '+esc(v.released):''} · ${esc(v.id)}</small></div></div>`).join('')||'<div class="empty">ничего не нашёл</div>';
  $('#vres').querySelectorAll('.vn').forEach(el=>el.onclick=()=>slot?showImages(el.dataset.vn,el.dataset.title):applyAll(el.dataset.vn));}catch(e){$('#vres').innerHTML='<div class="empty err">ошибка сети</div>';}};
 $('#vgo').onclick=search;$('#vq').addEventListener('keydown',e=>{if(e.key==='Enter')search();});search();}
// ---- archives tab
async function loadArchives(){try{const j=await(await fetch('/api/archives')).json();const a=j.archives||[];$('#archTotal').textContent=a.length?`${a.length} · ${fmt(a.reduce((s,x)=>s+x.size,0))}`:'пусто';
 $('#archives').innerHTML=a.map(x=>`<div class="arch"><div class="nm"><b>${esc(x.name)}</b><div class="path">${esc(x.disk)} · ${fmt(x.size)} · ${when(x.time)}</div></div><span class="acts">${x.extracted?'<span class="pill">распакован</span>':x.archive?`<button class="ghost sm" data-aextract="${esc(x.path)}">распаковать</button>`:x.part?'<span class="pill k">часть архива</span>':'<span class="pill k">файл</span>'}<button class="danger sm" data-adel="${esc(x.path)}" data-name="${esc(x.name)}">удалить</button></span></div>`).join('')||'<div class="empty">Архивов нет.</div>';}catch(e){}}
$('#archCleanup').onclick=async()=>{const r=await ask({title:'Удалить распакованные архивы',text:'Будут удалены только те архивы, для которых уже есть папка игры.',ok:'Удалить',danger:true});if(!r)return;const j=await api('/api/archive/cleanup',{});if(j.ok){toast(j.removed.length?'Удалено: '+j.removed.join(', '):'Нечего удалять','ok');loadArchives();}};
$('#inboxClear').onclick=async()=>{let st={};try{st=await(await fetch('/api/inbox/stats')).json();}catch(e){}
 if(!st.removed){toast(st.skipped?'Во входящих только то, что качается прямо сейчас':'Входящие и так пустые','ok');return;}
 const r=await ask({title:'Очистить входящие',text:`Удалить из _inbox всё: ${st.removed} шт., ${fmt(st.freed)}. Это архивы (распакованные и нет), отдельные файлы и остатки недокачанного. Папки игр не трогаются.`+(st.skipped?` То, что качается прямо сейчас (${st.skipped}), останется.`:''),ok:'Удалить всё',danger:true});if(!r)return;
 const j=await api('/api/inbox/clear',{});if(j.ok){toast(`Удалено: ${j.removed} · освобождено ${fmt(j.freed)}`,'ok');loadArchives();}};
// ---- settings tab (each control saves on change)
function fillSettings(){const s=lastState;if(!s)return;const st=s.settings||{},c=s.cdp||{};
 $('#sCdpTxt').textContent=c.available?'Управление Steam активно: имена, Proton и обложки применяются сразу.':c.marker&&c.enabled?'Управление Steam включится после одной перезагрузки дека. До этого имена и Proton встают в очередь и применятся сами.':c.enabled?'Папка Steam не найдена, управление Steam недоступно.':'Управление Steam выключено: игры добавляются через steamos-add-to-steam, имя и Proton придётся ставить вручную.';
 $('#sCompat').innerHTML=compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('');
 $('#sLinux').checked=!!st.prefer_linux;$('#sCef').checked=!!st.cef_enabled;$('#sVndb').checked=!!st.vndb_auto;$('#sSafe').checked=!st.vndb_nsfw;
 if(document.activeElement!==$('#sPw'))$('#sPw').value=(st.archive_passwords||[]).join(', ');if(document.activeElement!==$('#sUpd'))$('#sUpd').value=st.update_url||'';
 $('#sProxyView').textContent=st.proxy_masked||'не задан';$('#sProxyDl').checked=!!st.proxy_downloads;$('#sMega').checked=!!st.mega_verify;
 $('#sInfo').innerHTML=`DeckDrop v${esc(s.version)} · ${s.ffmpeg?'ffmpeg найден':'ffmpeg не найден'}<br>${(s.urls||[]).map(esc).join(' · ')}<br>`+(s.disks||[]).map(d=>`${esc(d.label)}: ${fmt(d.free)} свободно из ${fmt(d.total)} · <code>${esc(d.root)}</code>`).join('<br>')+(s.pending?`<br>В очереди до включения управления Steam: ${s.pending}`:'');}
document.querySelectorAll('#pgSettings [data-set]').forEach(el=>el.addEventListener('change',async()=>{const k=el.dataset.set;let v=el.type==='checkbox'?el.checked:el.value;if(el.dataset.invert)v=!v;
 if(k==='archive_passwords')v=v.split(',').map(x=>x.trim()).filter(Boolean);const j=await api('/api/settings',{[k]:v});if(j.ok){toast('Сохранено','ok');lastState.settings=j.settings;lastState.cdp=j.cdp;fillSettings();}}));
$('#sProxyEdit').onclick=async()=>{const p=await ask({title:'Настройки прокси',text:'Адрес прокси может содержать логин и пароль, поэтому он под PIN.',fields:[{label:'PIN',type:'password',numeric:true}],ok:'Показать'});if(!p)return;
 const rv=await api('/api/settings/reveal',{pin:p[0]});if(!rv.ok)return;
 const r=await ask({title:'Прокси для запросов DeckDrop',text:'socks5://хост:порт или http://хост:порт, можно с логином. Пустое поле выключает прокси.',fields:[{label:'Адрес',value:rv.proxy||'',placeholder:'socks5://192.168.1.10:10808'}],ok:'Сохранить'});if(!r)return;
 const j=await api('/api/settings',{proxy:r[0],pin:p[0]});if(j.ok){toast(r[0]?'Прокси сохранён':'Прокси выключен','ok');lastState.settings=j.settings;fillSettings();}};
$('#sTest').onclick=async()=>{const b=$('#sTest');b.disabled=true;b.textContent='проверяю…';$('#sTestRes').textContent='';
 try{const j=await(await fetch('/api/vndb/test')).json();
  $('#sTestRes').innerHTML=(j.proxy?`Прокси: <code>${esc(j.proxy)}</code><br>`:'Прокси не задан<br>')
   +j.checks.map(c=>`${c.ok?'✅':'❌'} ${esc(c.host)} ${esc(c.mode)}: ${c.ok?(c.ms+' мс'+(c.note?' · '+esc(c.note):'')):esc(c.error)}`).join('<br>');
  }catch(e){$('#sTestRes').textContent='не смог проверить';}
 b.disabled=false;b.textContent='Проверить связь с VNDB';};
$('#sPinGo').onclick=async()=>{const j=await api('/api/settings/pin',{old:$('#sPinOld').value,new:$('#sPinNew').value});if(j.ok){toast('PIN изменён','ok');$('#sPinOld').value=$('#sPinNew').value='';}};
// ---- media
async function mediaEnter(){if(mediaToken){loadMedia();return;}const st=await(await fetch('/api/media/status')).json();const a=$('#mediaAuth');$('#mediaBody').hidden=true;
 if(!st.set){a.innerHTML=`<div class="card auth"><div class="lock">🔐</div><h3 style="margin:0">Первый вход в галерею</h3><p>Придумай пароль. Его будут спрашивать при каждом открытии вкладки. Сбросить можно кнопкой внизу страницы по PIN.</p>
  <div class="row"><input id="pw1" type="password" placeholder="пароль, от 4 символов"></div><div class="row"><input id="pw2" type="password" placeholder="ещё раз"><button id="pwset">Сохранить</button></div></div>`;
  $('#pwset').onclick=async()=>{const p1=$('#pw1').value,p2=$('#pw2').value;if(p1!==p2){toast('Пароли не совпадают','err');return;}const r=await fetch('/api/media/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p1})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pw2').addEventListener('keydown',e=>{if(e.key==='Enter')$('#pwset').click();});}
 else{a.innerHTML=`<div class="card auth"><div class="lock">🔒</div><h3 style="margin:0">Галерея дека</h3><p>Скриншоты и записи Steam, экспортированные видео и картинки.</p><div class="row"><input id="pw" type="password" placeholder="пароль медиа"><button id="pwgo">Войти</button></div></div>`;
  const go=async()=>{const r=await fetch('/api/media/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pwgo').onclick=go;$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')go();});setTimeout(()=>$('#pw').focus(),60);}}
async function mediaOpen(){$('#mediaAuth').innerHTML='';$('#mediaBody').hidden=false;await loadMedia();}
async function loadMedia(rescan){const r=await fetch('/api/media/list'+(rescan?'?refresh=1':''),{headers:{'X-Media-Token':mediaToken}});if(r.status===401){mediaToken=null;mediaEnter();return;}const j=await r.json();mediaItems=j.items;
 $('#mhint').textContent=mediaItems.length+' файлов'+(j.ffmpeg?'':' · клипы Steam без ffmpeg склеиваются без звука');renderMedia();}
function renderMedia(){const q=$('#mq').value.trim().toLowerCase();const list=mediaItems.filter(i=>(mediaFilter==='all'||(mediaFilter==='image'?i.kind==='image':i.kind!=='image'))&&(!q||(i.game+' '+i.name).toLowerCase().includes(q)));
 $('#mgrid').innerHTML=list.slice(0,400).map(i=>`<div class="tile" data-id="${i.id}"><img loading="lazy" src="/media/${i.id}/thumb?t=${mediaToken}" alt=""><span class="k${i.kind==='image'?'':' v'}">${i.kind==='image'?'фото':i.kind==='clip'?'клип':'видео'}</span><div class="cap"><b>${esc(i.game)}</b><span class="muted">${when(i.time)} · ${fmt(i.size)}</span></div></div>`).join('')||'<div class="empty">Ничего не найдено.</div>';
 if(list.length>400)$('#mhint').textContent+=' · показаны первые 400';}
$('#mq').oninput=renderMedia;$('#mrefresh').onclick=()=>loadMedia(true);
document.querySelectorAll('.chips button[data-f]').forEach(b=>b.onclick=()=>{mediaFilter=b.dataset.f;document.querySelectorAll('.chips button[data-f]').forEach(x=>x.classList.toggle('on',x===b));renderMedia();});
$('#mgrid').addEventListener('click',e=>{const t=e.target.closest('.tile');if(!t)return;const i=mediaItems.find(x=>x.id===t.dataset.id);if(!i)return;viewing=i;
 const src=`/media/${i.id}?t=${mediaToken}`;$('#vtitle').textContent=i.game+' · '+i.name;$('#vopen').href=src;$('#vdl').href=src+'&dl=1';$('#vdl').setAttribute('download',i.name);
 $('#vbody').innerHTML=i.kind==='image'?`<img src="${src}">`:`<video src="${src}" controls playsinline autoplay></video>`;$('#viewer').classList.add('on');});
function closeViewer(){$('#viewer').classList.remove('on');$('#vbody').innerHTML='';viewing=null;}
$('#vclose').onclick=closeViewer;$('#viewer').addEventListener('click',e=>{if(e.target.id==='viewer'||e.target.id==='vbody')closeViewer();});
$('#vdel').onclick=async()=>{if(!viewing)return;const i=viewing;const r=await ask({title:'Удалить с дека',text:`${i.game} · ${i.name} (${fmt(i.size)}) будет удалён безвозвратно.${i.kind==='clip'?' Steam может показывать пустую запись до перезапуска.':''}`,fields:[{label:'PIN',type:'password',numeric:true}],ok:'Удалить',danger:true});if(!r)return;
 const j=await api('/api/media/delete',{id:i.id,pin:r[0],token:mediaToken});if(j.ok){toast('Удалено: '+j.removed,'ok');closeViewer();loadMedia(true);}};
$('#mediaReset').onclick=async()=>{const r=await ask({title:'Сбросить пароль галереи',text:'При следующем входе попросит придумать новый.',fields:[{label:'PIN',type:'password',numeric:true}],ok:'Сбросить',danger:true});if(!r)return;
 const j=await api('/api/media/reset',{pin:r[0]});if(j.ok){mediaToken=null;toast('Пароль сброшен','ok');if(tab==='media')mediaEnter();}};
// ---- self update
$('#update').onclick=async()=>{const r=await ask({title:'Обновить утилиту',text:'Ссылка на свежий deckdrop.py: GitHub или своя раздача с ПК (python -m http.server 8000 в папке с файлом).',fields:[{label:'Откуда взять свежий deckdrop.py',value:(lastState&&lastState.settings&&lastState.settings.update_url)||''},{label:'PIN',type:'password',numeric:true}],ok:'Обновить'});if(!r)return;
 const b=$('#update');b.disabled=true;b.textContent='обновляю…';const old=lastState&&lastState.version;
 const j=await api('/api/update',{url:r[0],pin:r[1]});
 if(!j.ok){b.disabled=false;b.textContent='Обновить утилиту';return;}
 toast(j.note,'ok');b.textContent=j.note;if(!/->/.test(j.note)){setTimeout(()=>{b.disabled=false;b.textContent='Обновить утилиту';},3000);return;}
 const np=j.new_port&&String(j.new_port)!==(location.port||'80')?j.new_port:null;const target=np?`${location.protocol}//${location.hostname}:${np}/`:null;
 if(target){toast('Новый адрес: '+target,'ok');setTimeout(()=>location.href=target,4000);return;}
 let n=0;const t=setInterval(async()=>{n++;try{const s=await(await fetch('/api/state')).json();if(s.version!==old){clearInterval(t);b.textContent='готово: v'+s.version;toast('Обновлено до v'+s.version,'ok');setTimeout(()=>location.reload(),1500);}}catch(e){}if(n>40){clearInterval(t);b.disabled=false;b.textContent='Обновить утилиту';}},1000);};
