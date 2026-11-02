// Statystyki: wykresy z /api/stats (agregacja po stronie serwera), wnioski liczone z danych, tabele zwinięte.
(() => {
const RANGE_KEY = "hpc_stats_hours";
const LABEL = { 24: "ostatnich 24 godzin", 168: "ostatnich 7 dni", 720: "ostatnich 30 dni" };
const num = (v, d = 1) => Number(v).toLocaleString("pl-PL", { minimumFractionDigits: d, maximumFractionDigits: d });
const kwh = v => num(v, v >= 100 ? 0 : 1);                       // duże liczby bez ułamków — czytelniej
const zl = v => num(v, v >= 100 ? 0 : 2);
const $ = id => document.getElementById(id);
const note = (id, text) => { $(id).innerHTML = text ? HPC.insight(text, "") : ""; };
const flat = (n, v) => Array.from({ length: n }, () => v);

let hours = 168;
try { const saved = Number(localStorage.getItem(RANGE_KEY)); if ([24, 168, 720].includes(saved)) hours = saved; } catch (e) { /* tryb prywatny */ }

function heikoNotes(d) {
  const s = d.summary, m = d.meta, span = LABEL[d.hours];
  if (!s) { ["comfort", "energy", "model"].forEach(k => note(`take-${k}`, `Brak danych z ${span} — zbierają się co ${num(d.step_min || 15, 0)} minut.`)); return; }
  const below = s.hours_below_min === null ? "" : s.hours_below_min > 0
    ? ` Poniżej minimum ${num(m.room_min_c)} °C dom był ${num(s.hours_below_min)} godz.`
    : ` Żaden pokój nie spadł poniżej minimum ${num(m.room_min_c)} °C.`;
  note("take-comfort", `Z ${span}: średnio ${num(s.avg_indoor_c)} °C przy celu ${num(m.target_c)} °C, najzimniej ${num(s.coldest_room_c)} °C.${below}`);
  note("take-energy", `Z ${span}: ${kwh(s.energy_kwh)} kWh, czyli ok. ${zl(s.cost_pln)} zł` +
    (s.peak_share === null ? "." : `; z sieci w drogim szczycie ${num(s.peak_share * 100, 0)}% tego prądu.`) +
    (s.own_kwh > 0 ? ` ${kwh(s.own_kwh)} kWh dały panele i bateria — ten prąd liczymy po cenie, za jaką poszedłby do sieci.` : ""));
  note("take-model", s.model_within_half_deg_pct === null ? "Model nie ma jeszcze pomiarów do porównania."
    : `W ${s.model_within_half_deg_pct}% kroków model pomylił się o najwyżej 0,5 °C (średni błąd ${num(s.model_mean_abs_err_c, 2)} °C). ` +
      (s.model_within_half_deg_pct >= 80 ? "To dobry wynik." : "Poniżej 80% — przewidywania warto traktować ostrożnie."));
}

function renderHeiko(d) {
  heikoNotes(d);
  const t = d.t, n = t.length, s = d.series, m = d.meta;
  // Tła komfortu: poniżej minimum zimno, między minimum a celem ostrzeżenie, powyżej celu w porządku.
  const minC = m.room_min_c, celC = m.target_c;
  const bands = minC == null || celC == null ? undefined : [{ from: null, to: minC, color: "--band-cold" },
    { from: minC, to: celC, color: "--band-warn" }, { from: celC, to: null, color: "--band-ok" }];
  HPC.chart($("chart-comfort"), { unit: "°C", unit2: "°C na zewnątrz", bands, data: [t, s.indoor_c, s.min_room_c, flat(n, m.target_c), flat(n, m.room_min_c), s.outdoor_c],
    series: [{ label: "Średnia w pokojach", color: "--chart-avg" }, { label: "Najzimniejszy pokój", color: "--chart-cold", dash: [2, 3] },
      { label: "Cel", color: "--chart-target", dash: [6, 4], width: 1 }, { label: "Minimum", color: "--chart-min", dash: [6, 4], width: 1 },
      { label: "Na zewnątrz", color: "--chart-out", y2: true, width: 1.5, fill: true, gradient: true }] });
  // Słupki nakładane: najwyższy (całość) pod spodem, na nim sieć, na wierzchu tania taryfa — trzy pasy.
  const sum = (...cols) => cols[0].map((_, i) => cols.every(c => c[i] === null) ? null : cols.reduce((a, c) => a + (c[i] || 0), 0));
  const grid = sum(s.energy_offpeak_kwh, s.energy_peak_kwh), total = sum(grid, s.energy_own_kwh);
  HPC.chart($("chart-energy"), { unit: "kWh", data: [t, total, grid, s.energy_offpeak_kwh], yRange: (u, lo, hi) => [0, Math.max(hi, 0.1)],
    series: [{ label: "Z paneli i baterii", color: "--chart-out", kind: "bars", shown: s.energy_own_kwh },
      { label: "Z sieci w szczycie (drogo)", color: "--peak", kind: "bars", shown: s.energy_peak_kwh },
      { label: "Z sieci, tania taryfa", color: "--accent", kind: "bars", shown: s.energy_offpeak_kwh }] });
  HPC.chart($("chart-model"), { unit: "°C (zmierzone − przewidziane)", data: [t, s.model_err_c, flat(n, 0.5), flat(n, -0.5)],
    yRange: (u, lo, hi) => [Math.min(lo, -1), Math.max(hi, 1)],
    series: [{ label: "Błąd modelu", color: "--accent" }, { label: "+0,5", color: "--muted", dash: [6, 4], width: 1 }, { label: "−0,5", color: "--muted", dash: [6, 4], width: 1 }] });
  HPC.chart($("chart-plan"), { unit: "°C", data: [t, s.base_curve_c, s.plan_komfort_c, s.plan_ekonomia_c, s.water_c],
    series: [{ label: "Krzywa pompy", color: "--muted", dash: [6, 4] }, { label: "Plan Komfort", color: "--peak" },
      { label: "Plan Ekonomia", color: "--accent" }, { label: "Woda faktycznie", color: "--chart-out", width: 1.5 }] });
}

function renderAttic(d) {
  const s = d.summary, span = LABEL[d.hours];
  note("take-attic", !s ? `Brak danych z ${span}.` :
    `Z ${span}: średnio ${num(s.avg_indoor_c)} °C, klimatyzacja pracowała ${num(s.ac_on_h)} godz. i zużyła ${kwh(s.energy_kwh)} kWh (ok. ${zl(s.cost_pln)} zł).` +
    (s.window_open_h > 0 ? ` Okno było otwarte ${num(s.window_open_h)} godz.` : ""));
  HPC.chart($("chart-attic"), { unit: "°C", unit2: "moc, W", data: [d.t, d.series.indoor_c, d.series.setpoint_c, d.series.power_w],
    series: [{ label: "Temperatura na poddaszu", color: "--accent" }, { label: "Nastawa klimatyzacji", color: "--muted", dash: [6, 4] },
      { label: "Pobór mocy", color: "--chart-out", y2: true, kind: "step", fill: true, width: 1 }] });
}

const when = ts => { const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); };
const v = x => x === null || x === undefined ? "—" : x;

async function renderTables() {
  try {
    const [h, a] = await Promise.all([HPC.getJSON("/api/history/heiko?limit=100"), HPC.getJSON("/api/history/attic?limit=100")]);
    $("heiko-history").innerHTML = HPC.collapsibleTable(["Czas", "Średnia °C", "Najzimniejszy pokój °C", "Na zewnątrz °C", "Woda °C", "Prąd kWh", HPC.html(HPC.term("blad_modelu", "Błąd modelu °C"))],
      h.map(r => [when(r.ts), v(r.indoor_temp_c), v(r.min_room_c), v(r.outdoor_temp_c), v(r.water_temp_c), v(r.energy_kwh), v(r.model_err_c)]), { empty: "Brak jeszcze cykli." });
    $("attic-history").innerHTML = HPC.collapsibleTable(["Czas", "Faza", "Temperatura °C", "Nastawa °C", "Moc W", "Okno otwarte", "Zapis do klimatyzacji", "Koszt zł"],
      a.map(r => [when(r.ts), v(r.phase), v(r.indoor_temp_c), v(r.ac_cmd_setpoint), v(r.ac_power_w), r.window_open ? "tak" : "nie", r.wrote ? "tak" : "nie", v(r.attic_cost_pln)]), { empty: "Brak jeszcze cykli." });
  } catch (e) { $("heiko-history").textContent = "Błąd wczytywania tabel."; }
}

// Runy klimatyzacji (A2): kafle z ostatnich 30 dni + tabela runów; null to brak danych, nie zero.
const has = x => x !== null && x !== undefined;
const sumKey = (rows, k) => { const xs = rows.map(r => r[k]).filter(has); return xs.length ? xs.reduce((a, b) => a + b, 0) : null; };
const ZONES = { cheap: "tania", mid: "średnia", expensive: "droga" };
const tileHtml = (label, value) => `<div class="cwu-tile"><span class="cwu-tile-value">${value}</span><span class="cwu-tile-label">${HPC.esc(label)}</span></div>`;

async function renderRuns() {
  try {
    const d = await HPC.getJSON("/api/attic_runs?days=60");
    const since = Date.now() - 30 * 86400e3;
    const recent = d.runs.filter(r => Date.parse(r.start) >= since);
    const kwhSum = sumKey(recent, "energy_kwh"), plnSum = sumKey(recent, "cost_pln"), min = sumKey(recent, "duration_min");
    const pvRows = recent.filter(r => has(r.pv_share) && r.energy_kwh > 0);
    const pvKwh = pvRows.reduce((a, r) => a + r.energy_kwh, 0);
    const pvVal = pvKwh > 0 ? pvRows.reduce((a, r) => a + r.pv_share * r.energy_kwh, 0) / pvKwh : null;
    $("attic-runs-tiles").innerHTML = [
      tileHtml("runy", String(recent.length)),
      tileHtml("godziny pracy", min === null ? "—" : num(min / 60, 1)),
      tileHtml("kWh (suma)", kwhSum === null ? "—" : kwh(kwhSum)),
      tileHtml("zł (suma)", plnSum === null ? "—" : zl(plnSum)),
      tileHtml("PV %", pvVal === null ? "—" : num(pvVal * 100, 0)),
    ].join("");
    $("attic-runs").innerHTML = HPC.collapsibleTable(["Start", "Czas min", "kWh", "Zł", "PV %", "Średnia W", "Szczyt W", "Strefa", "T start °C", "T koniec °C", "ΔT °C", "°C/godz.", "kWh na °C", "Zewn. °C"],
      d.runs.map(r => [when(r.start), v(r.duration_min), r.energy_check === false ? `${v(r.energy_kwh)} (?)` : v(r.energy_kwh),
        r.split_complete !== true ? `${v(r.cost_pln)} (przybl.)` : v(r.cost_pln), r.pv_share == null ? "—" : num(r.pv_share * 100, 0), v(r.avg_w), v(r.peak_w),
        ZONES[r.zone] || v(r.zone), v(r.temp_start_c), v(r.temp_end_c), v(r.delta_c), v(r.rate_c_h), v(r.kwh_per_c), v(r.outdoor_c)]),
      { empty: "Brak zapisanych runów — pojawią się po ustawieniu encji mocy klimatyzacji i nocnym zapisie." });
  } catch (e) { $("attic-runs").textContent = "Błąd wczytywania runów klimatyzacji."; }
}

let loadSeq = 0;
async function load() {
  const seq = ++loadSeq;                                        // szybkie przełączanie zakresu: liczy się tylko ostatnie żądanie
  document.querySelectorAll(".range-switch button").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.hours) === hours)));
  try {
    const [h, a] = await Promise.all([HPC.getJSON(`/api/stats/heiko?hours=${hours}`), HPC.getJSON(`/api/stats/attic?hours=${hours}`)]);
    if (seq !== loadSeq) return;
    renderHeiko(h);
    renderAttic(a);
  } catch (e) { $("chart-comfort").textContent = "Błąd wczytywania statystyk."; }
}

$("term-model").innerHTML = HPC.term("model", "modelu");
$("term-curve").innerHTML = HPC.term("krzywa", "krzywą grzewczą");
document.querySelector(".range-switch").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-hours]");
  if (!b) return;
  hours = Number(b.dataset.hours);
  try { localStorage.setItem(RANGE_KEY, String(hours)); } catch (e) { /* brak pamięci przeglądarki — zakres po prostu nie zostanie zapamiętany */ }
  load();
});
load();
renderTables();
renderRuns();
})();
