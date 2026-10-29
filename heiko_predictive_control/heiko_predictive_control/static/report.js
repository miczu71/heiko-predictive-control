// Raport doradcy D1 — wyłącznie odczyt. Wszystkie fetch-e względne (ingress).
const esc = HPC.esc;
const fmt = v => (v === null || v === undefined) ? "—" : (typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v);

// Tabele zwijają się do 5 wierszy (przycisk „Pokaż wszystkie”); wiersze od najnowszego.
const table = (head, rows, opts) => HPC.collapsibleTable(head, rows, opts);

// Słupki z podpisami wartości; `series` = [{label, values}] — więcej niż jedna seria układa się jedna na drugiej.
function svgBars(title, labels, series, unit) {
  if (!labels.length) return "";
  const W = 600, H = 190, L = 36, R = 8, T = 12, B = 30, n = labels.length;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const hi = Math.max(...totals, 0.001), bw = (W - L - R) / n;
  const y = v => T + (1 - v / hi) * (H - T - B);
  let bars = `<text class="bar-label" x="${L - 4}" y="${T + 4}" text-anchor="end">${fmt(Number(hi.toPrecision(2)))}</text>` +
    `<text class="bar-label" x="${L - 4}" y="${H - B}" text-anchor="end">0</text>`;
  labels.forEach((lab, i) => {
    let base = 0;
    series.forEach((s, k) => {
      const v = s.values[i] || 0;
      bars += `<rect class="bar-fill${k ? "-" + (k + 1) : ""}" x="${(L + i * bw + bw * 0.12).toFixed(1)}" width="${(bw * 0.76).toFixed(1)}" ` +
        `y="${y(base + v).toFixed(1)}" height="${(y(base) - y(base + v)).toFixed(1)}"><title>${esc(lab)} °C · ${esc(s.label)}: ${fmt(v)} ${esc(unit)}</title></rect>`;
      base += v;
    });
    bars += `<text class="bar-label" x="${(L + i * bw + bw / 2).toFixed(1)}" y="${H - 14}" text-anchor="middle">${esc(lab)}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${bars}` +
    `<text class="bar-label" x="${W / 2}" y="${H - 2}" text-anchor="middle">temperatura zewnętrzna, °C</text></svg>` +
    `<figcaption><span class="sc-title">${esc(title)}, ${esc(unit)}</span>` +
    (series.length > 1 ? series.map((s, k) => `<span><i class="sc-keydot bar-key-${k + 1}"></i>${esc(s.label)}</span>`).join("") : "") + `</figcaption>`;
}
// Klucze klas temperatury są liczbami jako napisy ("-3", "0", "12"): Object.entries ułożyłoby ujemne na końcu.
const byTemp = obj => Object.entries(obj).sort((a, b) => Number(a[0]) - Number(b[0]));
const put = HPC.put;

function renderReport(payload) {
  const r = payload.report;
  const meta = document.getElementById("report-meta");
  if (!r) {
    meta.textContent = payload.running ? "Liczenie raportu w toku…" : "Brak raportu — kliknij „Przelicz raport”.";
    return;
  }
  meta.textContent = `Policzony ${r.created} · sezon ${r.window.start} – ${r.window.end}` +
    (payload.running ? " · trwa przeliczanie…" : "") + (payload.error ? ` · błąd: ${payload.error}` : "");
  put("report-ideas",
    `<h4>Hipotezy do sprawdzenia (to nie są propozycje zmian)</h4><ul>${(r.ideas || []).map(i => `<li>${esc(i)}</li>`).join("") || "<li>brak</li>"}</ul>` +
    `<h4>Czego brakuje w danych</h4><ul>${(r.data_gaps || []).map(i => `<li>${esc(i)}</li>`).join("")}</ul>`);
  const c = r.comfort;
  put("report-comfort", !c ? "Brak danych." :
    `<p class="sub">Cel ${c.target}°C, pasmo dzień ±${c.band_day} / noc ±${c.band_night}, minimum pokoju ${c.room_min}°C · ${c.hours} godz. · ` +
    `najzimniejszy pokój: mediana ${fmt(c.coldest.p50)}, p5 ${fmt(c.coldest.p5)}, min ${fmt(c.coldest.min)}, godz. poniżej minimum: ${c.coldest.below_room_min_h}</p>` +
    table(["Pokój", "Godz.", "Średnia", "Od celu", "SD", "p5", "Mediana", "p95", "Min", "W paśmie"],
      [["ŚREDNIA STREF (to reguluje add-on)", c.hours, c.average.mean, c.average.offset, c.average.sd, null, null, null, null, `${(c.average.in_band * 100).toFixed(0)}%`],
       ...Object.entries(c.rooms).map(([k, v]) => [k, v.hours, v.mean, v.offset, v.sd, v.p5, v.p50, v.p95, v.min, v.in_band === null ? null : `${(v.in_band * 100).toFixed(0)}%`])]));
  const e = r.energy;
  put("report-energy", !e ? "Brak danych." :
    `<h4>Zużycie wg miesięcy</h4>` + table(["Miesiąc", "kWh", "Stopniodni", "kWh/stopniodzień", "Godz."],
      Object.entries(e.months).map(([k, v]) => [k, v.kwh, v.hdd, v.kwh_per_hdd, v.hours])));
  put("report-peak-class", !e || !e.peak_share ? "" :
    `<h4>Udział szczytu wg temperatury</h4>` +
    table(["Klasa temp. zewn. °C", "Udział szczytu", "kWh", "Godz."],
      byTemp(e.peak_share.by_class).map(([k, v]) => [k, v.share === null ? null : `${(v.share * 100).toFixed(0)}%`, v.kwh, v.hours]), { show: 6 }));
  const peak = e && e.peak_share ? byTemp(e.peak_share.by_class).filter(([, v]) => v.share !== null) : [];
  put("chart-peak", svgBars("Udział prądu pobranego w szczycie wg temperatury zewnętrznej", peak.map(([k]) => k),
    [{ label: "udział szczytu", values: peak.map(([, v]) => Math.round(v.share * 100)) }], "%"));
  put("report-peak-note", !e || !e.peak_share ? "" : HPC.insight(
    `${(e.peak_share.overall * 100).toFixed(0)}% prądu pompa pobrała w godzinach szczytu (${e.peak_share.kwh} kWh w ${e.peak_share.hours} godz.). ` +
    `Im mniej, tym taniej — Doradca stara się przesuwać grzanie na tańsze godziny.`));
  const h = r.pump_hours;
  put("report-hours", !h ? "Brak danych." :
    table(["Klasa temp. zewn. °C", "Dni", "Grzanie h/dobę", "CWU h/dobę", "Postój h/dobę"],
      byTemp(h.by_class).map(([k, v]) => [k, v.days, v.heating_h, v.dhw_h, v.standby_h]), { show: 6 }));
  const hc = h ? byTemp(h.by_class) : [];
  put("chart-hours", svgBars("Praca pompy na dobę wg temperatury zewnętrznej", hc.map(([k]) => k),
    [{ label: "grzanie", values: hc.map(([, v]) => v.heating_h) }, { label: "ciepła woda", values: hc.map(([, v]) => v.dhw_h) },
     { label: "postój", values: hc.map(([, v]) => v.standby_h) }], "h"));
  put("report-cop", !r.cop ? "Brak danych (latem pompa robi głównie CWU)." :
    table(["Klasa temp. zewn. °C", "Godz.", "Średni COP", "Min", "Max"],
      byTemp(r.cop).map(([k, v]) => [k, v.hours, v.mean, v.min, v.max]), { show: 6 }));
  const cp = r.cop ? byTemp(r.cop) : [];
  put("chart-cop", svgBars("Średni COP wg temperatury zewnętrznej", cp.map(([k]) => k), [{ label: "COP", values: cp.map(([, v]) => v.mean) }], "COP"));
  const o = r.own || {}, a = o.avg_per_day || {};
  put("report-own", `<p class="sub">Dób ze streszczeniami: ${o.days ?? 0}</p>` +
    table(["Starty sprężarki", "Praca sprężarki min", "Krótkie cykle", "Śr. cykl min", "Cykle CWU", "CWU min", "Grzanie min", "AH min", "HBH min", "Sygnał HWTBH min", "Impulsy P0", "kWh", "Udział szczytu"],
      [[a.compressor_starts, a.compressor_run_min, a.short_cycles, a.mean_run_min, a.dhw_cycles, a.dhw_min, a.heating_min, a.ah_min, a.hbh_min, a.hwtbh_min, a.p0_pulses, a.kwh, a.peak_share]]));
  put("report-p0", !(o.p0_days || []).length ? "Brak doby z wystarczającą liczbą próbek w standby." :
    table(["Doba", "P0 praca min", "P0 postój min", "Rozrzut °C", "Średnia stref °C", "Próbek (co 15 min)"],
      o.p0_days.map(d => [d.day, d.run_min ?? "?", d.stop_min ?? "?", d.spread_c, d.zones_avg_c, d.samples]), { show: 14 }));
}

async function loadReport(refresh) {
  try { renderReport(await HPC.getJSON(`/api/report${refresh ? "?refresh=1" : ""}`)); }
  catch (e) { put("report-meta", "Błąd wczytywania raportu."); }
}

async function loadChanges() {
  try {
    const d = await HPC.getJSON("/api/changes");
    const t = d.telemetry, s = d.summary;
    put("changes-summary",
      `<p class="sub">Zmiany parametrów pompy (dowolne źródło): dziś ${s.today} · 30 dni ${s.days30} · łącznie ${s.total}. ` +
      `Telemetria: ${t.rows} próbek, ostatnia ${t.last_ts ? new Date(t.last_ts * 1000).toLocaleString("pl-PL") : "—"}, ` +
      `baza ${(t.db_bytes / 1048576).toFixed(1)} MB${t.db_warn ? " — PRZEKROCZONO 200 MB" : ""}.</p>`);
    put("changes-list", table(["Czas", "Parametr", "Było", "Jest", "Źródło"], d.changes.map(c => [c.ts, c.key, c.old, c.new, c.source])));
  } catch (e) { put("changes-summary", "Błąd wczytywania zmian."); }
}

async function loadCatalog() {
  try {
    const d = await HPC.getJSON("/api/catalog");
    put("catalog", `<p class="sub">Znaleziono ${d.found} z ${d.total} parametrów w HA.</p>` +
      table(["Klasa", "Parametr", "Stan", "Zakres A", "Maks. krok", "Automatyzacja", "Wpływ"],
        d.params.map(p => [p.cls, p.label, p.state === undefined || p.state === null ? "brak encji" : `${p.state}${p.unit ? " " + p.unit : ""}`,
          p.domain === "number" ? `${p.class_a_range[0]}…${p.class_a_range[1]}` : (p.options.join(" / ") || "—"),
          p.max_step, p.managed_by_automation ? "tak" : "", p.impact])));
  } catch (e) { put("catalog", "Błąd wczytywania katalogu."); }
}

const pct = v => v === null || v === undefined ? null : `${(v * 100).toFixed(0)}%`;

function renderReplay(payload) {
  const r = payload.replay;
  if (!r) {
    put("replay-meta", payload.running ? "Liczenie odtworzenia w toku…" : "Brak odtworzenia — kliknij „Przelicz odtworzenie”.");
    return;
  }
  put("replay-meta", `Policzone ${r.created} · sezon ${r.window.start} – ${r.window.end}` +
    (payload.running ? " · trwa przeliczanie…" : "") + (payload.error ? ` · błąd: ${payload.error}` : ""));
  if (!r.ok) { put("replay-summary", `<p class="sub">${esc(r.error || "Brak danych.")}</p>`); return; }
  const t = r.thresholds;
  put("replay-summary",
    `<p class="sub">Doba oceniana o ${r.decision_hour}:00 na statystykach z poprzednich 24 h, przy założeniu, że krzywa była włączona. ` +
    `Dni ocenione: ${r.days_evaluated}. Reguła: −1, gdy średnia stref ≥ cel ${t.target_c} + ${t.down_avg_margin_c} i najzimniejszy pokój ≥ minimum ${t.room_min_c} + ${t.down_min_margin_c}; ` +
    `+1, gdy średnia ≤ cel − ${t.up_avg_margin_c} albo najzimniejszy pokój < minimum + ${t.up_min_margin_c}.</p>` +
    table(["Kierunek", "Dni", "Udział dni", "Zmiana kosztu zł/dobę", "Zmiana energii kWh/dobę", "Suma zł w sezonie", "Suma kWh w sezonie"],
      [["Obniżenie krzywej o 1", r.down.days, pct(r.down.share), r.down.cost_delta_pln_day, r.down.energy_delta_kwh_day, r.down.cost_delta_pln_season, r.down.energy_delta_kwh_season],
       ["Podniesienie krzywej o 1", r.up.days, pct(r.up.share), r.up.cost_delta_pln_day, r.up.energy_delta_kwh_day, r.up.cost_delta_pln_season, r.up.energy_delta_kwh_season]]) +
    `<p class="sub">${esc(r.caveat)}</p>`);
  put("replay-class", table(["Klasa temp. zewn. °C", "Dni z grzaniem", "Z propozycją −1", "Z propozycją +1"],
    byTemp(r.by_class).map(([k, v]) => [k, v.days, v.down, v.up])));
  put("replay-samples", `<h3>Ostatnie dni z propozycją</h3>` + table(["Dzień", "Kierunek", "Średnia stref °C", "Najzimniejszy (p5) °C", "Zewn. °C", "Δ koszt zł/dobę", "Δ energia kWh/dobę"],
    r.samples.map(s => [s.day, s.side === "down" ? "−1" : "+1", s.avg24_c, s.cold24_c, s.outdoor_c, s.effects.cost_day_delta_pln, s.effects.energy_day_delta_kwh])));
}

async function loadReplay(refresh) {
  try { renderReplay(await HPC.getJSON(`/api/advisor/replay${refresh ? "?refresh=1" : ""}`)); }
  catch (e) { put("replay-meta", "Błąd wczytywania odtworzenia."); }
}

// Ciepła woda ma własną stronę /cwu; stare linki #cwu przekierowują tam.
if (location.hash === "#cwu") location.replace(`${window.APP_BASE}/cwu`);
const tabs = [...document.querySelectorAll(".subtabs button")];
function showTab(name) {
  const tab = tabs.find(b => b.dataset.tab === name) || tabs[0];
  tabs.forEach(b => b.setAttribute("aria-selected", String(b === tab)));
  document.querySelectorAll(".subpane").forEach(p => { p.hidden = p.dataset.pane !== tab.dataset.tab; });
}
tabs.forEach(b => b.addEventListener("click", () => { history.replaceState(null, "", `#${b.dataset.tab}`); showTab(b.dataset.tab); }));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
showTab(location.hash.slice(1));
const termInto = (id, key) => { const el = document.getElementById(id); if (el) el.innerHTML = HPC.term(key); };
termInto("t-cop", "cop");

document.getElementById("replay-refresh")?.addEventListener("click", async () => {
  await loadReplay(true);
  const timer = setInterval(async () => {
    const p = await HPC.getJSON("/api/advisor/replay");
    renderReplay(p);
    if (!p.running) clearInterval(timer);
  }, 10000);
});

document.getElementById("report-refresh")?.addEventListener("click", async () => {
  await loadReport(true);
  const timer = setInterval(async () => {
    const p = await HPC.getJSON("/api/report");
    renderReport(p);
    if (!p.running) clearInterval(timer);
  }, 10000);
});
loadReport(false); loadChanges(); loadCatalog(); loadReplay(false);
