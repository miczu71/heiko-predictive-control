// Doradca — propozycje i decyzje w trybie próbnym (bez zapisu do pompy). Wszystkie fetch-e względne (ingress).
const esc = HPC.esc;
const num = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("pl-PL", { maximumFractionDigits: d });
const signed = (v, d = 2) => (v === null || v === undefined) ? "—" : `${v > 0 ? "+" : ""}${num(v, d)}`;
const put = HPC.put;

const KIND = { zmiana: "Zmiana", test: "Propozycja testu", eksperyment: "Eksperyment (dawny)", "cofnięcie": "Cofnięcie", alert: "Alert" };
const LENSES = [["komfort", "Komfort"], ["ekonomia", "Oszczędność"], ["obie", "Alerty i wskazówki"]];
const EFFECTS = [
  ["cost_day_delta_pln", "koszt energii (po wycenie ciepła zabranego z domu)", v => `${signed(v)} zł/dobę`],
  ["energy_day_delta_kwh", "energia pompy (po wycenie ciepła zabranego z domu)", v => `${signed(v)} kWh/dobę`],
  ["end_temp_delta_c", "temperatura domu po 36 h", v => `${signed(v)}°C`],
  ["mean_temp_delta_c", "średnia temperatura pokoi", v => `${signed(v)}°C`],
  ["min_temp_c", "najniższa przewidywana średnia", v => `${num(v, 1)}°C`],
  ["time_shift_pln_day", "informacyjnie — przesuwanie grzania w czasie (osobno, nie wchodzi do sumy)", v => `${signed(-v)} zł/dobę`],
];

// Dowody po polsku; nieznany klucz pokazuje się w surowej postaci.
const EVIDENCE = {
  avg24_c: "średnia temperatura stref z 24 h (°C)", target_c: "cel (°C)", cold24_c: "najzimniejszy pokój z 24 h (°C)",
  coldest_room: "najzimniejszy pokój", room_min_c: "minimum w pokoju (°C)", model_identified: "model domu rozpoznany",
  cooling_after_c: "o tyle ostygłby dom po obniżeniu (°C)", test_kind: "proponowany test", days: "dni danych",
  cycles_per_day: "cykli CWU na dobę", mean_cycle_min: "średni cykl (min)", peak_share: "udział szczytu w CWU",
  samples: "próbek", pv_covered: "próbek pokrytych przez PV/baterię", entity: "czujnik", day: "doba", value: "wartość",
  median: "zwykle (mediana)", limit: "próg alarmu", history_days: "dni historii",
};
const ANALYZER = { curve: "krzywa grzewcza", dhw: "ciepła woda", anomaly: "anomalie" };
const evLabel = k => EVIDENCE[k] || k;
const evValue = v => v === true ? "tak" : v === false ? "nie" : typeof v === "number" ? num(v) : v;

function card(p) {
  const change = p.to_value === null || p.to_value === undefined ? "" :
    `<div class="change">${esc(p.label || p.param_key)}: <strong>z ${num(p.from_value)} na ${num(p.to_value)}</strong> ${esc(p.unit)}</div>`;
  const effects = EFFECTS.filter(([k]) => p.effects && p.effects[k] !== undefined && p.effects[k] !== null)
    .map(([k, label, fmt]) => `<li>${esc(label)}: ${esc(fmt(p.effects[k]))}</li>`).join("");
  const evidence = Object.entries(p.evidence || {}).map(([k, v]) => `<li>${esc(evLabel(k))}: ${esc(evValue(v))}</li>`).join("");
  const alert = p.kind === "alert", isTest = p.kind === "test";
  return `<article class="proposal" data-id="${p.id}">
    <header><span class="badge kind-${esc(p.kind)}">${esc(KIND[p.kind] || p.kind)}</span>
      ${isTest ? "" : `<span class="badge conf-${esc(p.confidence)}">pewność: ${esc(p.confidence)}</span>`}
      <span class="sub">jeszcze ${p.remaining_h === null ? "—" : num(p.remaining_h, 1)} h</span></header>
    ${change}
    <p>${esc(p.reason)}</p>
    ${effects ? `<ul class="effects">${effects}</ul>` : ""}
    ${evidence ? `<details><summary>Na czym to opieram</summary><ul>${evidence}</ul></details>` : ""}
    <div class="actions">
      ${isTest ? `<a class="button" href="${HPC.base}/tests#${esc((p.evidence || {}).test_kind || "")}">Przejdź do testu</a>`
        : `<button type="button" data-decision="zatwierdzona">${alert ? "Przyjmij do wiadomości" : "Zatwierdź (próbnie)"}</button>`}
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
  put("advisor-meta", `Oczekujących: ${data.pending.length}. Powiadomienia na telefon: ${lens === "żaden" ? "wyłączone" : `tylko z grupy „${esc(lens)}”`}. ` +
    "Propozycje odświeżają się co godzinę, ta sama propozycja nie dubluje się.");
}

function renderHistory(data) {
  if (!data.history.length) { put("advisor-history", "Brak historii."); return; }
  put("advisor-history", HPC.collapsibleTable(["Utworzona", "Dotyczy", "Parametr", "Zmiana", "Rodzaj", "Status", "Decyzja", "Pewność"],
    data.history.map(p => [p.created.replace("T", " "), ANALYZER[p.analyzer] || p.analyzer, p.label || p.param_key,
      p.to_value === null ? "—" : `${num(p.from_value)} na ${num(p.to_value)}`, KIND[p.kind] || p.kind, p.status,
      (p.decided_at || "").replace("T", " ") || "—", p.confidence])));
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
