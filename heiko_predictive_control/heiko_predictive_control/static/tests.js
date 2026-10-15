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
const STATUS = { "w_toku": "w toku", "zakończony": "zakończony", "przerwany": "przerwany" };
let data = null;

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
    ${p ? `<p><span class="badge ok">wykonany ${when(p.ended)} · ${p.verdict ? `werdykt ${esc(p.verdict)} · ` : ""}zapisy ${p.confirmed}/${p.writes} potwierdzone</span></p>
    <details><summary>Szczegóły</summary>${body}</details>` : body}
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
      const outcome = t.abort_reason || (r ? `zapisy ${r.confirmed}/${r.writes} potwierdzone` +
        (r.variants ? `; ${VERDICT_NOTE(r)}` : "") +
        (r.max_confirm_min !== null ? `, maks. ${r.max_confirm_min} min` : "") +
        (r.excitation ? `; przejścia ${r.excitation.transitions}, spadek ${r.excitation.mean_drop_c}°C` : "") +
        (r.refit ? `; model: ${r.refit.reason}` : "") : "—");
      return [when(t.started), kind ? kind.title : t.kind, STATUS[t.status] || t.status, when(t.ended), outcome];
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
  try { render(await HPC.getJSON("/api/tests")); focusLinkedTest(); } catch (e) { put("tests-meta", "Błąd wczytywania testów."); }
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
load();
setInterval(() => { if (!document.hidden && !document.querySelector("button[data-armed]")) load(); }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
