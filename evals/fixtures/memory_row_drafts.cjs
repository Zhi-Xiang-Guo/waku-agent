// Run real memory handlers and the real render guard; replace only DOM/HTTP.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const [dir, scenario] = process.argv.slice(2);
const stored = {1: 'Meeting at 09:00.', 2: 'Lunch at 12:00.'};
const rows = new Map(), editors = new Map(), writes = [], alerts = [];
let response = {ok: true}, resolveSave;
const ctx = {console, location: {hash: '#memory/semantic'},
  needsSetup: () => false, alert: msg => alerts.push(msg)};
ctx.window = ctx;
function row(id, content) {
  let text = content;
  const cell = {
    get textContent() { return text; },
    set textContent(value) { text = value; editors.delete('ef-' + id); },
    set innerHTML(html) {
      const match = html.match(/<textarea[^>]*id="([^"]+)"[^>]*>([\s\S]*)<\/textarea>/);
      assert.ok(match, 'editor markup must contain a textarea');
      editors.set(match[1], {value: match[2].replaceAll('&lt;', '<'), focus() {}});
    }
  };
  return {dataset: {}, querySelector: sel => sel === '.fc' ? cell : null,
    lastElementChild: {innerHTML: `<button onclick="editFact(${id})">edit</button>`}};
}
function rebuild() {
  rows.clear(); editors.clear();
  for (const [id, content] of Object.entries(stored)) rows.set('fact-' + id, row(id, content));
}
const view = {set innerHTML(value) { rebuild(); }};
const inert = {textContent: '', scrollTop: 0};
ctx.document = {
  body: {classList: {remove() {}}},
  getElementById: id => rows.get(id) || editors.get(id) || (id === 'view' ? view : inert),
  querySelectorAll: () => [],
  querySelector: sel => sel === 'main' ? inert :
    sel === 'textarea[id^="ef-"]' ? (editors.values().next().value || null) : null
};
vm.createContext(ctx);
function run(code) { return vm.runInContext(code, ctx); }
for (const file of ['util.js', 'ui.js', 'memory.js']) {
  run(fs.readFileSync(`${dir}/${file}`, 'utf8'));
}
// Load the whole render function and its globals, without starting timers/UI wiring.
const main = fs.readFileSync(`${dir}/main.js`, 'utf8');
run(main.slice(0, main.indexOf('let lastFetch =')));
ctx.VIEWS = {memory: () => 'table', settings: () => 'settings'};
ctx.refresh = () => {
  ctx.data = {facts: Object.entries(stored).map(([id, content]) => ({id, content})),
    episodes: [], stats: {}, calendar: [], outbox: []};
  run('D = data; render()');
};
ctx.postJSON = async (url, body) => {
  assert.equal(url, '/api/memory');
  writes.push({...body});
  if (scenario === 'network') throw Error('offline');
  if (scenario === 'late') await new Promise(resolve => { resolveSave = resolve; });
  if (response.ok) stored[body.id] = body.content;
  return response;
};
function click(id, label) {
  const html = rows.get('fact-' + id).lastElementChild.innerHTML;
  const handlers = [...html.matchAll(/onclick="([^"]+)"[^>]*>([^<]*)<\/button>/g)];
  const button = handlers.find(m => m[2] === label);
  assert.ok(button, `${label} button on fact ${id}`);
  return run(button[1]);
}
function edit(id, value) { run(`editFact(${id})`); editors.get('ef-' + id).value = value; }
(async () => {
  ctx.refresh();
  edit(1, 'Meeting at 10:30. UNSAVED CHANGE');
  if (scenario !== 'single') edit(2, 'Lunch at 13:00.');
  const first = editors.get('ef-1');
  if (scenario === 'save' || scenario === 'cancel') {
    const second = editors.get('ef-2');
    await click(2, scenario);
    ctx.refresh(); // A timer-driven refresh must still protect the other draft.
    assert.equal(editors.get('ef-1'), first, 'saving/cancelling another row destroyed the draft');
    assert.equal(first.value, 'Meeting at 10:30. UNSAVED CHANGE');
    assert.equal(stored[1], 'Meeting at 09:00.', 'the other draft must not be saved implicitly');
    assert.equal(editors.has('ef-2'), false, 'the completed row should leave edit mode');
    assert.equal(stored[2], scenario === 'save' ? second.value : 'Lunch at 12:00.');
    assert.equal(rows.get('fact-2').querySelector('.fc').textContent, stored[2]);
    await click(1, 'save');
    assert.equal(stored[1], first.value, 'the remaining draft must still save');
    assert.equal(run('editing'), false, 'polling resumes after the last editor closes');
    assert.equal(editors.size, 0);
    assert.equal(writes.length, scenario === 'save' ? 2 : 1);
  } else if (scenario === 'single') {
    await click(1, 'save');
    assert.equal(stored[1], first.value);
    assert.equal(editors.size, 0);
    assert.equal(run('editing'), false);
  } else if (['error', 'missing', 'network'].includes(scenario)) {
    response = scenario === 'missing' ? {ok: false} : {error: 'write failed'};
    try { await click(2, 'save'); } catch (e) { assert.equal(e.message, 'offline'); }
    ctx.refresh();
    assert.equal(editors.get('ef-1'), first, 'a failed save must preserve other drafts');
    assert.equal(editors.get('ef-2').value, 'Lunch at 13:00.');
    assert.equal(stored[2], 'Lunch at 12:00.');
  } else if (scenario === 'late') {
    const saving = click(2, 'save');
    // Navigation destroys the old editor; returning opens a different edit.
    ctx.location.hash = '#settings'; ctx.refresh();
    ctx.location.hash = '#memory/semantic'; ctx.refresh();
    edit(2, 'New draft after navigation');
    const newEditor = editors.get('ef-2');
    resolveSave(); await saving; ctx.refresh();
    assert.equal(editors.get('ef-2'), newEditor, 'an old response must not close a new editor');
    assert.equal(newEditor.value, 'New draft after navigation');
  }
  console.log(JSON.stringify({case: scenario, passed: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
