// waku dashboard — inline Memory/SOUL/skill editing actions.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

function editFact(id){
  const row = document.getElementById("fact-"+id); if(!row) return;
  editing = true;
  const cell = row.querySelector(".fc"); const cur = cell.textContent;
  row.dataset.factContent = cur;
  row.dataset.factActions = row.lastElementChild.innerHTML;
  cell.innerHTML = `<textarea class="editor" id="ef-${id}">${cur.replace(/</g,"&lt;")}</textarea>`;
  const act = row.lastElementChild;
  act.innerHTML = uiButton("save", {level: "primary", size: "sm", onclick: `saveFact(${id})`})
    + " " + uiButton("cancel", {level: "tertiary", size: "sm", onclick: `cancelFact(${id})`});
  document.getElementById("ef-"+id).focus();
}
async function saveFact(id){
  const editor = document.getElementById("ef-"+id);
  const v = editor.value.trim();
  const r = await postJSON("/api/memory", {action:"update_fact", id, content:v});
  if (!r.ok){ alert("Error: " + (r.error || "Fact could not be saved.")); return; }
  // A response from an old view must not close an editor opened since then.
  if (document.getElementById("ef-"+id) !== editor) return;
  finishFactEdit(id, v);
}
function cancelFact(id){
  const row = document.getElementById("fact-"+id); if (!row) return;
  finishFactEdit(id, row.dataset.factContent);
}
function finishFactEdit(id, content){
  const row = document.getElementById("fact-"+id);
  row.querySelector(".fc").textContent = content;
  row.lastElementChild.innerHTML = row.dataset.factActions;
  delete row.dataset.factContent;
  delete row.dataset.factActions;
  // Keep the other row editors intact until each is saved or cancelled.
  editing = !!document.querySelector('textarea[id^="ef-"]');
  refresh();
}
async function delMem(action, id){
  if(!confirm("Delete this from memory?")) return;
  await postJSON("/api/memory", {action, id});
  refresh();
}
// dirty-state: a Save button stays muted until its editor actually changes
function dirty(btnId){ editing = true; const b = document.getElementById(btnId); if (b) b.disabled = false; }
async function saveSoul(){
  const v = document.getElementById("soul").value;
  const r = await postJSON("/api/memory", {action:"save_soul", content:v});
  document.getElementById("soul-msg").textContent = r.error ? ("Error: "+r.error) : "Saved — live next turn.";
  if (!r.error){ const b=document.getElementById("soul-save"); if(b) b.disabled=true; editing=false; }
}
async function saveSkill(i){
  const ta = document.getElementById("sk-"+i);
  const r = await postJSON("/api/memory", {action:"save_skill", path:ta.dataset.path, content:ta.value});
  document.getElementById("skmsg-"+i).textContent = r.error ? ("Error: "+r.error) : "Saved — live next turn.";
  if (!r.error){ const b=document.getElementById("sksave-"+i); if(b) b.disabled=true; editing=false; }
}
