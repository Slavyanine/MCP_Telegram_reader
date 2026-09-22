"use strict";
const $ = id => document.getElementById(id);
let token = "", state = null, rows = new Map(), dirty = false, busy = false, reviewed = null;
const types = {channel:"Канал / супергруппа", group:"Группа", user:"Личный диалог"};
function message(text, error=false) { $("message").textContent=text; $("message").classList.toggle("error",error); $("message").hidden=false; }
async function api(operation, data={}) {
  const response = await fetch("/api/"+operation,{method:"POST",headers:{"Authorization":"Bearer "+token,"Content-Type":"application/json"},body:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok) throw new Error(result.error || "Операция не выполнена");
  return result;
}
async function action(fn) {
  if(busy) return;
  busy=true; document.querySelectorAll("button").forEach(x=>x.disabled=true);
  // Freeze fields while saving/applying so the review always matches the server draft.
  const fields=[...document.querySelectorAll("input,select")].map(x=>[x,x.disabled]);
  fields.forEach(([x])=>x.disabled=true);
  try { await fn(); } catch(error) { message(error.message,true); }
  finally { busy=false;document.querySelectorAll("button").forEach(x=>x.disabled=false);fields.forEach(([x,disabled])=>{if(x.isConnected)x.disabled=disabled;});if(state) {renderTable();modes();} }
}
function changed() { dirty=true;reviewed=null;$("review").hidden=true;$("draft-status").textContent="Есть несохранённые изменения"; }
function renderTable() {
  const query=$("search").value.toLocaleLowerCase(), kind=$("kind").value;
  $("chats").replaceChildren();let shown=0;
  for(const row of rows.values()) {
    if(kind && row.kind!==kind || ![row.name,row.title,row.key].join(" ").toLocaleLowerCase().includes(query)) continue;
    shown++;
    const tr=document.createElement("tr"), check=document.createElement("input");check.type="checkbox";check.checked=row.selected;check.disabled=!row.available;check.setAttribute("aria-label","Разрешить "+row.name);
    const selectCell=document.createElement("td");selectCell.append(check);tr.append(selectCell);
    const name=document.createElement("td"), caption=document.createElement("small");name.append(document.createTextNode(row.name));caption.textContent=types[row.kind]+(row.available?"":" · недоступен");name.append(caption);tr.append(name);
    for(const field of ["key","title"]) {
      const td=document.createElement("td"), input=document.createElement("input");input.value=row[field];input.maxLength=field==="key"?64:200;input.disabled=!row.selected;input.setAttribute("aria-label",field+" · "+row.name);
      if(field==="key") input.placeholder="project_a";
      input.addEventListener("input",()=>{row[field]=input.value;changed();});td.append(input);tr.append(td);
    }
    check.addEventListener("change",()=>{row.selected=check.checked;if(row.selected&&!row.key)row.key="chat_"+row.id.replace(":","_");changed();renderTable();});
    $("chats").append(tr);
  }
  $("empty").hidden=shown!==0;$("selection-count").textContent="Выбрано: "+[...rows.values()].filter(r=>r.selected).length;
}
function accept(next, merge=false) {
  if(merge && state && next.revision!==state.revision) throw new Error("Черновик изменён в другой вкладке. Обновите страницу перед продолжением.");
  state=next;
  if(typeof next.service_active === "boolean") $("service-status").textContent=next.service_active?"MCP-служба запущена · проверено при загрузке":"MCP-служба остановлена · проверено при загрузке";
  if(merge) { for(const row of next.chats){const existing=rows.get(row.id);rows.set(row.id,existing?{...row,selected:existing.selected,key:existing.key,title:existing.title}:row);} }
  else {rows=new Map(next.chats.map(r=>[r.id,r]));dirty=false;$("indexing").checked=next.settings.enabled;$("transcribe").checked=next.settings.transcribe;$("backend").value=next.settings.backend;$("yandex-key").value="";$("deepgram-key").value="";}
  $("api-id").value=next.api_id;
  $("account").textContent=next.account?`${next.account.name} · ID ${next.account.id}${next.account.premium?" · Premium":""}${next.new_account?" · подготовлен к переключению":""}`:"Аккаунт ещё не проверен. Загрузите список чатов.";
  $("conflict").hidden=!next.conflict;
  $("draft-status").textContent=dirty?"Есть несохранённые изменения":next.saved?"Черновик сохранён":"Рабочая конфигурация";
  $("load-chats").textContent=next.more?"Загрузить ещё чаты":"Список загружен";
  for(const p of ["yandex","deepgram"])$(p+"-status").textContent=next.keys[p]?"· настроен":"· не настроен";
  renderTable();modes();
}
function modes() {
  $("transcribe").disabled=!$("indexing").checked;
  $("backend").disabled=!$("indexing").checked||!$("transcribe").checked;
  const notes={telegram:"Telegram: текущая реализация требует Premium у аккаунта серверной сессии. Поддерживаются голосовые и видеосообщения-кружки.",yandex:"Yandex: голосовые передаются в SpeechKit. Текущий режим поддерживает mono OggOpus до 30 секунд и 1 МБ; кружки не поддерживаются.",deepgram:"Deepgram: голосовые передаются провайдеру. Действуют лимиты размера и длительности из конфигурации; кружки не поддерживаются."};
  $("provider-note").textContent=!$("indexing").checked?"Индексация выключена: фоновая транскрибация и локальный поиск не работают.":notes[$("backend").value];
  $("load-chats").disabled=busy||!state.more;
}
async function save() {
  const keys={};for(const p of ["yandex","deepgram"]){if($(p+"-key").value)keys[p]=$(p+"-key").value;}
  const next=await api("save",{revision:state.revision,chats:[...rows.values()].filter(r=>r.selected).map(({id,key,title})=>({id,key,title})),settings:{enabled:$("indexing").checked,transcribe:$("transcribe").checked,backend:$("backend").value},keys});
  accept(next);reviewed=null;$("review").hidden=true;
}
$("gate-form").addEventListener("submit",event=>{event.preventDefault();action(async()=>{token=$("token").value.trim();accept(await api("state"));$("token").value="";$("gate").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("message").hidden=true;});});
$("logout").addEventListener("click",()=>{token="";location.reload();});
$("search").addEventListener("input",renderTable);$("kind").addEventListener("change",renderTable);
for(const id of ["indexing","transcribe","backend"])$(id).addEventListener("change",()=>{changed();modes();});
for(const id of ["yandex-key","deepgram-key"])$(id).addEventListener("input",changed);
$("load-chats").addEventListener("click",()=>action(async()=>{message("Загружаю чаты Telegram…");accept(await api("dialogs"),true);message(state.more?"Страница чатов загружена. Можно загрузить следующую.":"Все доступные диалоги загружены.");}));
$("show-login").addEventListener("click",()=>{$("login-panel").hidden=false;$("phone-form").hidden=false;$("code-form").hidden=true;$("password-form").hidden=true;});
$("phone-form").addEventListener("submit",event=>{event.preventDefault();action(async()=>{const hash=$("api-hash").value;$("api-hash").value="";const result=await api("login/start",{api_id:Number($("api-id").value),api_hash:hash,phone:$("phone").value.trim()});$("phone-form").hidden=true;$("code-form").hidden=false;message(result.message);});});
async function finish(field) {
  const value=$(field).value;$(field).value="";const result=await api("login/finish",{[field]:value});
  if(result.step==="password"){$("code-form").hidden=true;$("password-form").hidden=false;message(result.message);}
  else {accept(result.state);reviewed=null;$("review").hidden=true;$("login-panel").hidden=true;$("phone").value="";message("Новый аккаунт подготовлен. Загрузите и выберите разрешённые чаты, затем примените настройки.");}
}
for(const field of ["code","password"])$(field+"-form").addEventListener("submit",event=>{event.preventDefault();action(()=>finish(field));});
$("cancel-login").addEventListener("click",()=>action(async()=>{await api("login/cancel");for(const field of ["api-hash","code","password","phone"])$(field).value="";$("login-panel").hidden=true;}));
$("save").addEventListener("click",()=>action(async()=>{await save();message("Черновик сохранён. Работающий сервер не изменён.");}));
$("discard").addEventListener("click",()=>{if(confirm("Удалить черновик и загрузить рабочую конфигурацию?"))action(async()=>{accept(await api("discard"));reviewed=null;$("review").hidden=true;$("login-panel").hidden=true;message("Черновик сброшен.");});});
$("preview").addEventListener("click",()=>action(async()=>{await save();reviewed=await api("preview");if(reviewed.revision!==state.revision){reviewed=null;throw new Error("Черновик изменён в другой вкладке. Обновите страницу.");}$("summary").replaceChildren();const describe=s=>`индексация ${s.enabled?"вкл":"выкл"}, транскрибация ${s.transcribe?"вкл":"выкл"}, ${s.backend}`;for(const text of [reviewed.new_account?"Будет переключён Telegram-аккаунт и создана отдельная база индекса.":"Telegram-аккаунт остаётся прежним.","Добавить: "+(reviewed.added.join(", ")||"нет"),"Исключить: "+(reviewed.removed.join(", ")||"нет"),"Изменить: "+(reviewed.edited.join(", ")||"нет"),"Заменить API-ключи: "+(reviewed.keys_changed.join(", ")||"нет"),"Было: "+describe(reviewed.before),"Станет: "+describe(reviewed.after)]){const p=document.createElement("p");p.textContent=text;$("summary").append(p);}$("review").hidden=false;$("review").scrollIntoView({behavior:"smooth"});message("Проверьте изменения перед перезапуском.");}));
$("cancel-review").addEventListener("click",()=>{reviewed=null;$("review").hidden=true;});
$("apply").addEventListener("click",()=>action(async()=>{if(!reviewed||dirty)throw new Error("Повторите проверку изменений.");message("Применяю настройки и проверяю запуск MCP. Дождитесь завершения…");const result=await api("apply",{revision:reviewed.revision});reviewed=null;$("review").hidden=true;accept(await api("state"));message(result.message);}));
window.addEventListener("beforeunload",event=>{if(dirty||busy){event.preventDefault();event.returnValue="";}});
