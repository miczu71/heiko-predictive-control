// Raport doradcy D1 — wyłącznie odczyt. Wszystkie fetch-e względne (ingress).
const esc = s => String(s ?? "—").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
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
const put = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };

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
}

const when = iso => new Date(iso).toLocaleString("pl-PL");
const cfgSwitch = v => v === null || v === undefined ? "?" : (v === "on" ? "WŁ" : v === "off" ? "WYŁ" : v);

function renderDhwCycles(payload) {
  const groups = payload.groups || [], cycles = payload.cycles || [];
  put("dhw-groups", `<h4>Grupy wg ustawień grzałki</h4>` +
    table(["Okres", HPC.html(HPC.term("slot49", "Grzałka przy CWU")), HPC.html(HPC.term("slot50", "Priorytet grzałki")),
           HPC.html(HPC.term("slot52", "Opóźnienie grzałki (min)")), "Cykle ≥19 min", "Z grzałką", "Mediana minuty wejścia", "kWh grzałki", "zł grzałki", "Mała próba"],
      groups.map(g => [`${when(g.first)} – ${when(g.last)}`, cfgSwitch(g.cfg_49), g.cfg_50 ?? "?", g.cfg_52 ?? "?", g.cycles, g.cycles_with_ah,
        g.median_ah_tick_min, g.kwh, g.pln, g.small_sample ? "tak" : ""]).reverse(), { empty: "Brak cykli." }));
  put("dhw-cycles", `<h4>Cykle (najnowsze u góry)</h4>` +
    table(["Start", "Typ", "Cel °C", "Woda start", "Woda min 19", "Deficyt", "T zewn.", "Czas min",
           "Minuta wejścia grzałki", "Grzałka min", "Grzałka kWh", "Grzałka zł", "Grzałka przy CWU", "Priorytet", "Opóźnienie"],
      cycles.map(c => [new Date(c.start).toLocaleString("pl-PL"), c.type, c.target_c, c.water_start_c,
        c.water_min19_c, c.deficit_c, c.outdoor_c, c.duration_min, c.ah_tick_min, c.ah_min, c.ah_kwh, c.ah_pln,
        cfgSwitch(c.cfg_49), c.cfg_50 ?? "?", c.cfg_52 ?? "?"])));
}

// Rozrzut: tempo grzania samej sprężarki wg temperatury zewnętrznej; kolor = czy w tym cyklu weszła AH.
function svgScatter(cycles) {
  const pts = cycles.filter(c => c.hp_rate_c_min != null && c.outdoor_c != null);
  if (pts.length < 2) return "";
  const W = 600, H = 200, L = 40, R = 8, T = 10, B = 24;
  const xs = pts.map(c => c.outdoor_c), ys = pts.map(c => c.hp_rate_c_min);
  const xlo = Math.floor(Math.min(...xs)) - 1, xhi = Math.ceil(Math.max(...xs)) + 1;
  const yhi = Math.max(0.2, Math.ceil(Math.max(...ys) / 0.2) * 0.2), ylo = Math.min(0, Math.floor(Math.min(...ys) / 0.2) * 0.2);
  const x = v => L + ((v - xlo) * (W - L - R)) / (xhi - xlo);
  const y = v => T + ((yhi - v) * (H - T - B)) / (yhi - ylo);
  const xstep = xhi - xlo > 20 ? 5 : 2;
  let grid = "";
  for (let v = ylo; v <= yhi + 1e-9; v += 0.2) grid += `<line class="pc-grid" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="pc-tick" x="${L - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end">${v.toFixed(1)}</text>`;
  for (let v = Math.ceil(xlo / xstep) * xstep; v <= xhi; v += xstep) grid += `<text class="pc-tick" x="${x(v).toFixed(1)}" y="${H - 6}" text-anchor="middle">${v}°</text>`;
  const dots = pts.map(c => `<circle class="sc-dot${c.ah_tick_min != null ? " sc-ah" : ""}" cx="${x(c.outdoor_c).toFixed(1)}" cy="${y(c.hp_rate_c_min).toFixed(1)}" r="5">` +
    `<title>${esc(when(c.start))} · zewn. ${c.outdoor_c}°C · woda ${fmt(c.hp_water_mid_c)}°C · ${c.hp_rate_c_min} °C/min</title></circle>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Tempo grzania sprężarki wg temperatury zewnętrznej">${grid}${dots}</svg>` +
    `<figcaption><span class="sc-title">Tempo grzania samej sprężarki (°C/min) wg temperatury zewnętrznej (°C)</span>` +
    `<span><i class="sc-keydot"></i>cykl bez AH</span><span><i class="sc-keydot sc-ah"></i>cykl z AH</span></figcaption>`;
}

function renderDhwModel(payload) {
  const m = payload.model || {}, a = payload.ah_effect || {};
  const coef = (name, fit) => [name, ...fit.coef.map(c => c.toFixed(3)), fit.r2.toFixed(2)];
  put("dhw-model", m.ok
    ? `<p class="sub">Model z ${m.n} odcinków sprężarki. Zakres uczenia: temp. zewn. ${m.ranges.outdoor[0]}…${m.ranges.outdoor[1]} °C, ` +
      `woda ${m.ranges.water[0]}…${m.ranges.water[1]} °C; poza nim (±3 °C) model nie wycenia AH.</p>` +
      table(["Wielkość", "Wyraz wolny", "na 1 °C zewn.", "na 1 °C wody", "R²"],
        [coef("Tempo grzania, °C/min", m.rate), coef("Moc sprężarki, kW", m.kw)])
    : `<p class="sub">Model jeszcze niegotowy: ${esc(m.reason || "brak danych")}. Odcinki zbierają się z kolejnych cykli CWU.</p>`);
  put("dhw-model-chart", svgScatter(payload.cycles || []));
  if (!a.cycles_with_ah) {
    put("dhw-ah-effect", `<p class="sub">W oknie nie było cyklu z grzałką AH — przy opóźnieniu startu źródła dodatkowego 30 min to normalne.</p>`);
    return;
  }
  const rows = (a.rows || []).map(r => [when(r.start), r.extra_kwh, r.extra_pln, r.saved_min, r.price_pln_kwh, ""]);
  const skipped = (a.skipped || []).map(s => [when(s.start), null, null, null, null, s.reason]);
  put("dhw-ah-effect",
    `<p class="sub">Cykli z AH: ${a.cycles_with_ah}, wycenionych: ${a.evaluated}. ` +
    (!a.evaluated ? "Żadnego cyklu nie da się jeszcze wycenić — powody w kolumnie „Uwagi”."
      : `Grzałka dołożyła ${a.extra_kwh} kWh (${a.extra_pln} zł) i skróciła grzanie o ${a.saved_min} min` +
        `${a.pln_per_saved_min === null ? "" : ` — ${a.pln_per_saved_min} zł za zaoszczędzoną minutę`}.`) + `</p>` +
    table(["Wejście AH", "Dodatkowe kWh", "Dodatkowe zł", "Zaoszczędzone min", "Cena zł/kWh", "Uwagi"], [...rows, ...skipped]));
}

async function loadDhwCycles() {
  try {
    const payload = await HPC.getJSON("/api/dhw_cycles");
    renderDhwCycles(payload);
    renderDhwModel(payload);
  } catch (e) { put("dhw-groups", "Błąd wczytywania cykli CWU."); }
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
loadReport(false); loadChanges(); loadCatalog(); loadReplay(false); loadDhwCycles();
