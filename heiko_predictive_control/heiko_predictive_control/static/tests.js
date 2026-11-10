// Testy uruchamiane przez usera. Start wymaga drugiego kliknięcia (potwierdzenie harmonogramu).
const esc = HPC.esc;
const put = HPC.put;
const shift = v => (v === null || v === undefined) ? "—" : `${v > 0 ? "+" : ""}${Number(v).toLocaleString("pl-PL")}°C`;
const when = iso => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString("pl-PL", { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
};
const val = (v, stop) => (stop === null || stop === undefined) ? shift(v) : `${v}/${stop}`;     // para P0 „5/5” albo przesunięcie
const isP0 = w => (w.entity_id || "").includes("circulation_pump_p0");
const VERDICT_NOTE = r => r.verdict
  ? `werdykt ${r.verdict}${r.gain_c !== undefined ? ` (rozrzut pokoi przy 5/5 ${r.gain_c >= 0 ? "niższy" : "wyższy"} o ${Math.abs(r.gain_c)}°C` : ""}` +
    `${r.extra_kwh_day !== undefined ? `, ${r.extra_kwh_day > 0 ? "+" : ""}${r.extra_kwh_day} kWh/dobę` : ""}` +
    `${r.gain_c !== undefined ? ")" : ""}, pewność ${r.confidence}`
  : "bez werdyktu (za mało próbek w standby)";
const nf = v => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("pl-PL", { maximumFractionDigits: 2 });
const mval = m => (m && typeof m === "object") ? nf(m.value) + (m.n ? ` <span class="sub">(n=${m.n})</span>` : "") : nf(m);
// Wynik testu z definicji (result.arms): tabela wariant × miara + werdykt. Trzy wbudowane mają własny opis (VERDICT_NOTE).
function armsTable(r) {
  const arms = Object.keys(r.arms || {}).sort();
  if (!arms.length) return "";
  const names = [...new Set(arms.flatMap(a => Object.keys(r.arms[a].measures || {})))];
  const v = r.verdict;
  const note = v ? (v.winner ? `lepszy wariant ${esc(v.winner)} (Δ ${nf(v.gain)}), pewność ${esc(v.confidence)}` : `bez rozstrzygnięcia: ${esc(v.reason)}`) : "";
  return `<table class="day-table arms-table"><thead><tr><th>Wariant</th><th>Doby</th>${names.map(n => `<th>${esc(n)}</th>`).join("")}</tr></thead><tbody>` +
    arms.map(a => `<tr${v && v.winner === a ? ' class="winner"' : ""}><th scope="row">${esc(a)}</th><td>${r.arms[a].days}</td>${names.map(n => `<td>${mval(r.arms[a].measures[n])}</td>`).join("")}</tr>`).join("") +
    `</tbody></table>${note ? `<p class="sub">${note}</p>` : ""}`;
}
const STATUS = { "w_toku": "w toku", "zakończony": "zakończony", "przerwany": "przerwany" };
let data = null;
const builtin = new Set();

function stepsList(schedule, done) {
  return `<ol class="steps">${schedule.map((s, i) => {
    const cls = done === undefined ? "" : i < done ? " done" : i === done ? " next" : "";
    return `<li class="step${cls}"><time>${when(s.at)}</time><span>${val(s.value, s.stop)}</span></li>`;
  }).join("")}</ol>`;
}

function renderActive(t) {
  if (!t) { put("tests-active", ""); return; }
  const kind = data.kinds.find(k => k.key === t.kind);
  const waiting = t.pending_since ? ` · czekam na potwierdzenie z pompy (zapis ${when(t.pending_since)}, limit ${data.confirm_min} min)` : "";
  put("tests-active", `<article class="test-active" aria-live="polite">
    <header><span class="badge kind-test">w toku</span><strong>${esc(kind ? kind.title : t.kind)}</strong></header>
    <p class="sub">Start ${when(t.started)} · wartość wyjściowa ${val(t.baseline, t.baseline_stop)} · teraz zlecone ${val(t.expected, t.expected_stop)}${waiting}</p>
    ${stepsList(t.schedule, t.step)}
    <div class="actions"><button type="button" class="danger" data-abort>Przerwij i przywróć ${val(t.baseline, t.baseline_stop)}</button>
      <span class="decision-status" role="status"></span></div></article>`);
}

function renderKinds() {
  put("tests-kinds", data.kinds.map(k => {
    const p = k.passed, label = p ? "Powtórz" : "Start";
    const body = `<p>${esc(k.description)}</p>
    <p class="sub">Wynik: ${esc(k.measure)}</p>
    ${k.schedule.length ? `<details${!p && k.schedule.length <= 4 ? " open" : ""}><summary>Harmonogram (${k.schedule.length} kroków)</summary>${stepsList(k.schedule)}</details>` : ""}
    <details><summary>Kiedy test się przerwie</summary><ul class="effects">${(k.abort_conditions || data.abort_conditions).map(c => `<li>${esc(c)}</li>`).join("")}</ul></details>`;
    return `<div class="lens-col"><article class="proposal test-kind${p ? " passed" : ""}" data-kind="${esc(k.key)}">
    <h3>${esc(k.title)}</h3>
    ${p ? `<p><span class="badge ok">wykonany ${when(p.ended)} · ${p.verdict ? `werdykt ${esc(p.verdict)} · ` : ""}${k.key === "bezwladnosc" ? (p.identified ? "model zidentyfikowany · " : "model niezidentyfikowany · ") : ""}zapisy ${p.confirmed}/${p.writes} potwierdzone</span></p>
    <details><summary>Szczegóły</summary>${body}</details>` : body}
    <div class="actions def-actions"><button type="button" class="link" data-def-copy="${esc(k.key)}">Kopiuj</button>${builtin.has(k.key) ? "" : `<button type="button" class="link" data-def-edit="${esc(k.key)}">Edytuj</button><button type="button" class="link danger" data-def-del="${esc(k.key)}">Usuń</button>`}</div>
    <div class="actions">${k.blocked
      ? `<button type="button" disabled>${label}</button><span class="blocked">${esc(k.blocked)}</span>`
      : `<button type="button" data-start="${esc(k.key)}" data-label="${label}">${label}</button><span class="decision-status" role="status"></span>`}
    </div></article></div>`;
  }).join(""));
}

function renderHistory() {
  put("tests-history", HPC.collapsibleTable(["Start", "Test", "Status", "Koniec", "Przyczyna / wynik"], data.history.map(t => {
      const kind = data.kinds.find(k => k.key === t.kind);
      const r = t.result;
      const generic = r && r.arms && !r.variants && !r.excitation;
      const outcome = t.abort_reason || (generic ? { html: armsTable(r) } : r ? `zapisy ${r.confirmed}/${r.writes} potwierdzone` +
        (r.variants ? `; ${VERDICT_NOTE(r)}` : "") +
        (r.max_confirm_min !== null ? `, maks. ${r.max_confirm_min} min` : "") +
        (r.excitation ? `; przejścia ${r.excitation.transitions}, spadek ${r.excitation.mean_drop_c}°C` : "") +
        (r.refit ? `; model: ${r.refit.reason}` : "") : "—");
      return [when(t.started), kind ? kind.title : (t.def && t.def.title) || t.kind, STATUS[t.status] || t.status, when(t.ended), outcome];
    }), { empty: "Żaden test jeszcze nie był uruchomiony." }));
  put("tests-writes", HPC.collapsibleTable(["Czas", "Test", "Zmiana", "HA przyjęło", "Potwierdzenie z pompy"],
    data.writes.map(w => [when(w.ts), w.test_id ? `#${w.test_id}` : "—", isP0(w) ? `${w.entity_id.includes("_run_time") ? "P0 praca" : "P0 postój"} ${w.old} → ${w.new} min` : `${shift(w.old)} → ${shift(w.new)}`, w.ok ? "tak" : "nie",
      w.confirmed_at === null ? "czekam…" : w.confirmed_at.includes("T") ? when(w.confirmed_at) : w.confirmed_at]),
    { empty: "Add-on jeszcze nic nie zapisał do pompy." }));
}

function render(d) {
  data = d;
  put("tests-meta", `${HPC.term("przesuniecie", "Przesunięcie krzywej")} teraz: <strong>${shift(d.shift)}</strong> · ${HPC.term("krzywa", "krzywa grzewcza")}: ` +
    `${d.curve_on === null ? "nieznana" : d.curve_on ? "włączona" : "wyłączona"} · pompa P0 (praca/postój): ` +
    `<strong>${d.p0 ? `${d.p0[0]}/${d.p0[1]} min` : "—"}</strong> · zapis do pompy: ` +
    `${d.enabled ? "dozwolony" : "wyłączony w Opcjach"}`);
  renderActive(d.active);
  renderKinds();
  renderHistory();
}

// Link z Doradcy (tests#bezwladnosc): po pierwszym wyrenderowaniu przewiń do karty testu i podświetl ją.
let hashHandled = false;
function focusLinkedTest() {
  if (hashHandled) return;
  hashHandled = true;
  const card = location.hash.length > 1 && document.querySelector(`.test-kind[data-kind="${CSS.escape(location.hash.slice(1))}"]`);
  if (card) { card.classList.add("is-linked"); card.scrollIntoView({ block: "center" }); }
}

async function load() {
  try {
    const defs = await HPC.getJSON("/api/test-defs");
    builtin.clear(); defs.definitions.filter(d => d.builtin).forEach(d => builtin.add(d.key));
    render(await HPC.getJSON("/api/tests")); focusLinkedTest();
  } catch (e) { put("tests-meta", "Błąd wczytywania testów."); }
}

async function post(path, statusEl) {
  try {
    const resp = await fetch(`${HPC.base}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const result = await resp.json();
    if (result.kinds) render(result);
    if (!result.ok && statusEl && statusEl.isConnected) statusEl.textContent = result.error || `Błąd HTTP ${resp.status}`;
    else if (!result.ok) put("tests-meta", esc(result.error || `Błąd HTTP ${resp.status}`));
  } catch (e) {
    if (statusEl) statusEl.textContent = "Błąd połączenia z add-onem.";
  }
}

document.addEventListener("click", async ev => {
  const start = ev.target.closest("button[data-start]");
  if (start) {
    const status = start.parentElement.querySelector(".decision-status");
    if (!start.dataset.armed) {               // pierwsze kliknięcie uzbraja, drugie startuje
      const kind = data.kinds.find(k => k.key === start.dataset.start);
      start.dataset.armed = "1";
      start.textContent = `Potwierdź start (${kind.schedule.length} kroków)`;
      status.textContent = "Kliknij ponownie w ciągu 8 s.";
      setTimeout(() => { if (start.isConnected) { delete start.dataset.armed; start.textContent = start.dataset.label; status.textContent = ""; } }, 8000);
      return;
    }
    start.disabled = true;
    status.textContent = "Uruchamiam…";
    await post(`/api/tests/${encodeURIComponent(start.dataset.start)}/start`, status);
    return;
  }
  const abort = ev.target.closest("button[data-abort]");
  if (abort) {
    abort.disabled = true;
    const status = abort.parentElement.querySelector(".decision-status");
    status.textContent = "Przerywam…";
    await post("/api/tests/abort", status);
  }
});
// ── Edytor definicji (E5): JSON + walidacja i podgląd na żywo; zapis przez /api/test-defs ──
const ed = { key: null, timer: null, seq: 0 };            // key = edytowana własna definicja (PUT); null = nowa (POST)
const $ = id => document.getElementById(id);
const TEMPLATE = {
  key: "moj_test", title: "Mój test", description: "", requires: { heiko_enabled: true }, coexist: ["g13_peak"],
  days: { filter: "peak_day", count: 10 }, design: "alternate", order: ["B", "A"],
  arms: { A: { overrides: { g13_peak_preheat_shift: 1 } }, B: { overrides: { g13_peak_preheat_shift: 0 } } },
  measures: [{ name: "floor_cost", window: "13:00→13:00", per: "hdd" }, "safety_trips"],
  verdict: { primary: "floor_cost", better: "lower", min_n: 4 },
};

function setStatus(html, cls) { const el = $("def-status"); el.className = "sub" + (cls ? " " + cls : ""); el.innerHTML = html; }

function renderPreview(res) {
  const errs = res.errors || [];
  if (errs.length) {
    setStatus(`<strong>Niepoprawna definicja:</strong><ul class="effects">${errs.map(e => `<li>${esc(e)}</li>`).join("")}</ul>`, "bad");
    put("def-preview", ""); $("def-save").disabled = true; return;
  }
  const p = res.preview || {};
  const days = Object.entries(p.arms_by_day || {});
  setStatus(p.blocked ? `Definicja poprawna, ale start zablokowany: ${esc(p.blocked)}` : "Definicja poprawna.", p.blocked ? "" : "good");
  $("def-save").disabled = false;
  put("def-preview", `${days.length ? `<h4>Doby wariantów (najbliższe 14 dni)</h4><ol class="steps arms-days">${days.map(([d, a]) => `<li class="step"><time>${new Date(d + "T12:00:00").toLocaleDateString("pl-PL", { weekday: "short", day: "numeric", month: "numeric" })}</time><span>${esc(a)}</span></li>`).join("")}</ol>` : ""}
    ${p.schedule && p.schedule.length ? `<h4>Zapisy do pompy (${p.schedule.length})</h4>${stepsList(p.schedule)}` : `<p class="sub">Test nie zapisuje do pompy${days.length ? " — tylko nakłada opcje add-onu na dobę wariantu" : ""}.</p>`}
    ${p.end_at ? `<p class="sub">Koniec testu: ${when(p.end_at)}</p>` : ""}`);
}

async function validateNow() {
  const seq = ++ed.seq, text = $("def-json").value;
  let body;
  try { body = JSON.parse(text); } catch (e) { setStatus(`Niepoprawny JSON: ${esc(e.message)}`, "bad"); put("def-preview", ""); $("def-save").disabled = true; return; }
  try {
    const resp = await fetch(`${HPC.base}/api/test-defs/validate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const res = await resp.json();
    if (seq !== ed.seq) return;                                   // nowsza walidacja już trwa
    if (!resp.ok && !res.errors) { setStatus(esc(res.error || `Błąd HTTP ${resp.status}`), "bad"); return; }
    renderPreview(resp.ok ? res : { errors: res.errors });
  } catch (e) { if (seq === ed.seq) setStatus("Błąd połączenia z add-onem.", "bad"); }
}

function openEditor(defn, key, title) {
  ed.key = key;
  $("def-editor").hidden = false; $("def-editor").open = true;
  $("def-title").textContent = title;
  $("def-json").value = JSON.stringify(defn, null, 2);
  $("def-key-note").textContent = key ? `Zapis zastąpi własną definicję „${key}”. Klucza nie da się zmienić — zmień go, żeby zapisać kopię.` : "Zapis doda nową definicję. Dodanie niczego nie uruchamia — start robisz sam na karcie testu.";
  $("def-editor").scrollIntoView({ block: "start" });
  validateNow();
}

async function fetchDef(key) {
  const r = await HPC.getJSON(`/api/test-defs/${encodeURIComponent(key)}`);
  return r.definition;
}

async function saveDef() {
  let body;
  try { body = JSON.parse($("def-json").value); } catch (e) { setStatus("Niepoprawny JSON.", "bad"); return; }
  const edit = ed.key && body.key === ed.key;
  const url = edit ? `/api/test-defs/${encodeURIComponent(ed.key)}` : "/api/test-defs";
  $("def-save").disabled = true;
  try {
    const resp = await fetch(`${HPC.base}${url}`, { method: edit ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const res = await resp.json();
    if (!resp.ok || !res.ok) { renderPreview({ errors: res.errors || [res.error || `Błąd HTTP ${resp.status}`] }); return; }
    ed.key = body.key;
    setStatus(`Zapisano „${esc(body.key)}”.`, "good");
    await load();
  } catch (e) { setStatus("Błąd połączenia z add-onem.", "bad"); $("def-save").disabled = false; }
}

async function deleteDef(key, btn) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1"; btn.textContent = "Na pewno usunąć?";
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = "Usuń"; } }, 6000);
    return;
  }
  const resp = await fetch(`${HPC.base}/api/test-defs/${encodeURIComponent(key)}`, { method: "DELETE" });
  const res = await resp.json().catch(() => ({}));
  if (!resp.ok) { put("tests-meta", esc((res.errors || [res.error || `Błąd HTTP ${resp.status}`])[0])); return; }
  if (ed.key === key) { ed.key = null; $("def-editor").hidden = true; }
  await load();
}

async function exportDefs() {
  const blob = new Blob([JSON.stringify(await HPC.getJSON("/api/test-defs/export"), null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "heiko-test-defs.json"; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function importDefs(file) {
  let doc;
  try { doc = JSON.parse(await file.text()); } catch (e) { put("tests-meta", "Plik nie jest poprawnym JSON."); return; }
  const list = Array.isArray(doc) ? doc : Array.isArray(doc.definitions) ? doc.definitions : [doc];
  const out = [];
  for (const defn of list) {
    const resp = await fetch(`${HPC.base}/api/test-defs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(defn) });
    const res = await resp.json().catch(() => ({}));
    out.push(resp.ok ? `✓ ${defn.key}` : `✗ ${defn && defn.key}: ${(res.errors || [res.error || resp.status])[0]}`);
  }
  put("tests-meta", `Import: ${out.map(esc).join(" · ") || "pusty plik"}`);
  await load();
}

document.addEventListener("input", ev => {
  if (ev.target.id === "def-json") { clearTimeout(ed.timer); ed.timer = setTimeout(validateNow, 400); }
});
document.addEventListener("click", async ev => {
  const t = ev.target.closest("button, a");
  if (!t) return;
  if (t.id === "def-new") openEditor(TEMPLATE, null, "Nowa definicja testu");
  else if (t.id === "def-save") saveDef();
  else if (t.id === "def-close") $("def-editor").hidden = true;
  else if (t.id === "def-export") exportDefs();
  else if (t.id === "def-import") $("def-file").click();
  else if (t.dataset.defCopy) { const d = await fetchDef(t.dataset.defCopy); openEditor({ ...d, key: `${d.key}_kopia`, title: `${d.title} (kopia)` }, null, "Kopia definicji"); }
  else if (t.dataset.defEdit) openEditor(await fetchDef(t.dataset.defEdit), t.dataset.defEdit, "Edycja definicji");
  else if (t.dataset.defDel) deleteDef(t.dataset.defDel, t);
});
document.addEventListener("change", ev => {
  if (ev.target.id === "def-file" && ev.target.files[0]) { importDefs(ev.target.files[0]); ev.target.value = ""; }
});

load();
setInterval(() => { if (!document.hidden && !document.querySelector("button[data-armed]")) load(); }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
