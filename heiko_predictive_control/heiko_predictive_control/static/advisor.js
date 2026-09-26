// Doradca D2 — propozycje i decyzje w trybie próbnym (bez zapisu do pompy). Wszystkie fetch-e względne (ingress).
const esc = s => String(s ?? "—").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("pl-PL", { maximumFractionDigits: d });
const signed = (v, d = 2) => (v === null || v === undefined) ? "—" : `${v > 0 ? "+" : ""}${num(v, d)}`;
const put = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };

const KIND = { zmiana: "Zmiana", eksperyment: "Eksperyment", "cofnięcie": "Cofnięcie", alert: "Alert" };
const LENSES = [["komfort", "Komfort"], ["ekonomia", "Oszczędność"], ["obie", "Alerty i wskazówki"]];
const EFFECTS = [
  ["cost_day_delta_pln", "koszt energii (po wycenie ciepła zabranego z domu)", v => `${signed(v)} zł/dobę`],
  ["energy_day_delta_kwh", "energia pompy (po wycenie ciepła zabranego z domu)", v => `${signed(v)} kWh/dobę`],
  ["end_temp_delta_c", "temperatura domu po 36 h", v => `${signed(v)}°C`],
  ["mean_temp_delta_c", "średnia temperatura pokoi", v => `${signed(v)}°C`],
  ["min_temp_c", "najniższa przewidywana średnia", v => `${num(v, 1)}°C`],
  ["time_shift_pln_day", "informacyjnie — przesuwanie grzania w czasie (osobno, pakiety dopiero w D5)", v => `${signed(-v)} zł/dobę`],
];

function card(p) {
  const change = p.to_value === null || p.to_value === undefined ? "" :
    `<div class="change">${esc(p.label || p.param_key)}: <strong>z ${num(p.from_value)} na ${num(p.to_value)}</strong> ${esc(p.unit)}</div>`;
  const effects = EFFECTS.filter(([k]) => p.effects && p.effects[k] !== undefined && p.effects[k] !== null)
    .map(([k, label, fmt]) => `<li>${esc(label)}: ${esc(fmt(p.effects[k]))}</li>`).join("");
  const evidence = Object.entries(p.evidence || {}).map(([k, v]) => `<li>${esc(k)}: ${esc(typeof v === "number" ? num(v) : v)}</li>`).join("");
  const alert = p.kind === "alert";
  return `<article class="proposal" data-id="${p.id}">
    <header><span class="badge kind-${esc(p.kind)}">${esc(KIND[p.kind] || p.kind)}</span>
      <span class="badge conf-${esc(p.confidence)}">pewność: ${esc(p.confidence)}</span>
      <span class="sub">jeszcze ${p.remaining_h === null ? "—" : num(p.remaining_h, 1)} h</span></header>
    ${change}
    <p>${esc(p.reason)}</p>
    ${effects ? `<ul class="effects">${effects}</ul>` : ""}
    ${evidence ? `<details><summary>Dowody</summary><ul>${evidence}</ul></details>` : ""}
    <div class="actions">
      <button type="button" data-decision="zatwierdzona">${alert ? "Przyjmij do wiadomości" : "Zatwierdź (próbnie)"}</button>
      <button type="button" class="secondary" data-decision="odrzucona">Odrzuć</button>
      <span class="decision-status" role="status"></span>
    </div></article>`;
}

function renderPending(data) {
  const lens = data.lens;
  const cols = LENSES.map(([key, title]) => {
    const items = data.pending.filter(p => p.lens === key);
    const active = key === lens ? " lens-active" : "";
    return `<div class="lens-col${active}"><h3>${esc(title)}${key === lens ? " · wyróżnione" : ""}</h3>` +
      (items.map(card).join("") || `<p class="sub">Brak propozycji.</p>`) + `</div>`;
  }).join("");
  put("advisor-pending", cols);
  put("advisor-meta", `Oczekujących: ${data.pending.length}. Powiadomienia: ${lens === "żaden" ? "wyłączone (soczewka „żaden”)" : `soczewka ${esc(lens)}`}. ` +
    "Propozycje odświeżają się co godzinę, ta sama propozycja nie dubluje się.");
}

function renderHistory(data) {
  if (!data.history.length) { put("advisor-history", "Brak historii."); return; }
  put("advisor-history", `<table><thead><tr>${["Utworzona", "Analizator", "Parametr", "Zmiana", "Rodzaj", "Status", "Decyzja", "Pewność"]
    .map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>` + data.history.map(p => `<tr><td>${esc(p.created.replace("T", " "))}</td>` +
    `<td>${esc(p.analyzer)}</td><td>${esc(p.label || p.param_key)}</td>` +
    `<td>${p.to_value === null ? "—" : `${num(p.from_value)} na ${num(p.to_value)}`}</td><td>${esc(KIND[p.kind] || p.kind)}</td>` +
    `<td>${esc(p.status)}</td><td>${esc((p.decided_at || "").replace("T", " ") || "—")}</td><td>${esc(p.confidence)}</td></tr>`).join("") +
    "</tbody></table>");
}

async function load() {
  try {
    const data = await HPC.getJSON("/api/proposals");
    renderPending(data);
    renderHistory(data);
  } catch (e) { put("advisor-meta", "Błąd wczytywania propozycji."); }
}

document.addEventListener("click", async ev => {
  const btn = ev.target.closest("button[data-decision]");
  if (!btn) return;
  const cardEl = btn.closest(".proposal");
  const status = cardEl.querySelector(".decision-status");
  cardEl.querySelectorAll("button").forEach(b => { b.disabled = true; });
  status.textContent = "Zapisuję…";
  try {
    const resp = await fetch(`${HPC.base}/api/proposals/${cardEl.dataset.id}/decision`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: btn.dataset.decision }) });
    const result = await resp.json();
    if (result.ok) { await load(); return; }
    status.textContent = result.error || `Błąd HTTP ${resp.status}`;
    await new Promise(r => setTimeout(r, 4000));
    await load();
  } catch (e) {
    status.textContent = "Błąd zapisu decyzji.";
    cardEl.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});
load();
