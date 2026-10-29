// Ciepła woda (CWU) — wyłącznie odczyt, przeniesione z raportu D1. Wszystkie fetch-e względne (ingress).
const esc = HPC.esc;
const fmt = v => (v === null || v === undefined) ? "—" : (typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v);
const table = (head, rows, opts) => HPC.collapsibleTable(head, rows, opts);
const put = HPC.put;

const when = iso => new Date(iso).toLocaleString("pl-PL");
const cfgSwitch = v => v === null || v === undefined ? "?" : (v === "on" ? "WŁ" : v === "off" ? "WYŁ" : v);
const hasVal = v => v !== null && v !== undefined;
const num = (v, d = 0) => hasVal(v) && !Number.isNaN(v) ? v.toFixed(d) : "—";
const pctTxt = v => hasVal(v) ? `${(v * 100).toFixed(0)}%` : "—";
const isAh = c => hasVal(c.ah_tick_min);
const ZONES = { cheap: "tania", mid: "średnia", expensive: "droga" };
const DAY_MS = 86400e3, WINDOW_DAYS = 30;

// Suma pola z pominięciem null (null to brak danych, nie zero); brak wszystkich wartości → null.
function sumOf(rows, key) {
  const vals = rows.map(c => c[key]).filter(hasVal);
  return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
}
function median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
// Okno kafli i wykresów 1–2: cykle z ostatnich 30 dni po `start`.
function recentCycles(cycles) {
  const since = Date.now() - WINDOW_DAYS * DAY_MS;
  return cycles.filter(c => Date.parse(c.start) >= since);
}

function tile(label, value, note) {
  return `<div class="cwu-tile"><span class="cwu-tile-value">${value}</span><span class="cwu-tile-label">${esc(label)}</span>` +
    (note ? `<span class="cwu-tile-note">${esc(note)}</span>` : "") + `</div>`;
}
// Notka tylko gdy pole ma braki: „z N z M runów”.
const partNote = (n, m) => n < m ? `z ${n} z ${m} runów` : "";

function renderTiles(recent) {
  const m = recent.length;
  const costRows = recent.filter(c => hasVal(c.cost_pln));
  const kwhN = recent.filter(c => hasVal(c.kwh_total)).length;
  const ahN = recent.filter(c => hasVal(c.ah_pln)).length;
  const costSum = sumOf(recent, "cost_pln");
  const pvRows = recent.filter(c => hasVal(c.pv_share) && hasVal(c.kwh_total));
  const pvKwh = pvRows.reduce((a, c) => a + c.kwh_total, 0);
  const pvVal = pvKwh > 0 ? pvRows.reduce((a, c) => a + c.pv_share * c.kwh_total, 0) / pvKwh : null;
  const pvN = recent.filter(c => hasVal(c.pv_share)).length;
  put("cwu-tiles", [
    tile("runy", String(m)),
    tile("mediana czasu runu, min", num(median(recent.map(c => c.duration_min).filter(hasVal)), 1)),
    tile("kWh (suma)", num(sumOf(recent, "kwh_total"), 1), partNote(kwhN, m)),
    tile("zł (suma)", num(costSum, 2), partNote(costRows.length, m)),
    tile("zł na run", costRows.length ? num(costSum / costRows.length, 2) : "—", partNote(costRows.length, m)),
    tile("runy z grzałką AH", m ? `${num((100 * recent.filter(isAh).length) / m, 0)}%` : "—"),
    tile("koszt grzałki AH, zł", num(sumOf(recent, "ah_pln"), 2), partNote(ahN, m)),
    tile("udział PV", pvVal === null ? "—" : `${num(pvVal * 100, 0)}%`, partNote(pvN, m)),
  ].join(""));
}

// Wykres 1: starty runów wg godziny doby (lokalnie), tło godziny = strefa taryfy z hour_levels.
function svgRunsByHour(recent, hourLevels) {
  const W = 600, H = 190, L = 36, R = 8, T = 12, B = 30, bw = (W - L - R) / 24;
  const levelOf = h => { const r = hourLevels.find(x => x.hour === h); return r ? r.level : null; };
  const plain = Array(24).fill(0), ah = Array(24).fill(0);
  recent.forEach(c => { const h = new Date(c.start).getHours(); if (isAh(c)) ah[h]++; else plain[h]++; });
  const hi = Math.max(1, ...plain.map((v, i) => v + ah[i]));
  const y = v => T + (1 - v / hi) * (H - T - B);
  let body = `<text class="bar-label" x="${L - 4}" y="${T + 4}" text-anchor="end">${hi}</text>` +
    `<text class="bar-label" x="${L - 4}" y="${H - B}" text-anchor="end">0</text>`;
  for (let h = 0; h < 24; h++) {
    const lvl = levelOf(h);
    if (Object.hasOwn(ZONES, lvl)) body += `<rect class="zone-${lvl}" x="${(L + h * bw).toFixed(1)}" y="${T}" width="${bw.toFixed(1)}" height="${(H - T - B).toFixed(1)}">` +
      `<title>${h}:00 · strefa ${ZONES[lvl]}</title></rect>`;
  }
  for (let h = 0; h < 24; h++) {
    const x = (L + h * bw + bw * 0.12).toFixed(1), w = (bw * 0.76).toFixed(1);
    const seg = (v, base, cls, name) => v ? `<rect class="${cls}" x="${x}" width="${w}" y="${y(base + v).toFixed(1)}" ` +
      `height="${(y(base) - y(base + v)).toFixed(1)}"><title>${h}:00 · ${name}: ${v}</title></rect>` : "";
    body += seg(plain[h], 0, "bar-fill", "bez AH") + seg(ah[h], plain[h], "bar-fill-2", "z AH");
    if (h % 3 === 0) body += `<text class="bar-label" x="${(L + h * bw + bw / 2).toFixed(1)}" y="${H - 14}" text-anchor="middle">${h}</text>`;
  }
  const zoneKeys = Object.keys(ZONES).filter(k => hourLevels.some(x => x.level === k))
    .map(k => `<span><i class="sc-keydot" style="background:var(--band-${k})"></i>strefa: ${ZONES[k]}</span>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Starty runów CWU wg godziny doby">${body}` +
    `<text class="bar-label" x="${W / 2}" y="${H - 2}" text-anchor="middle">godzina doby (lokalnie)</text></svg>` +
    `<figcaption><span class="sc-title">Starty runów wg godziny doby</span>` +
    `<span><i class="sc-keydot bar-key-1"></i>bez AH</span><span><i class="sc-keydot bar-key-2"></i>z AH</span>${zoneKeys}</figcaption>`;
}

// Wykres 2: czas runu wg temperatury zewnętrznej; kolor = z AH / bez AH; linia progu slotu 52 i linia modelu.
function svgDuration(recent, all, model) {
  const pts = recent.filter(c => hasVal(c.duration_min) && hasVal(c.outdoor_c));
  if (pts.length < 2) return "";
  const thrRow = all.find(c => hasVal(c.cfg_52));                 // lista cykli jest od najnowszego
  const thr = thrRow ? thrRow.cfg_52 : null;
  const sTarget = median(recent.filter(c => hasVal(c.target_c)).map(c => c.target_c));
  const sStart = median(recent.filter(c => hasVal(c.water_start_c)).map(c => c.water_start_c));
  let modelPts = [], modelNote = "model niegotowy";
  if (model.ok && sTarget !== null && sStart !== null) {
    const [c0, c1, c2] = model.rate.coef;
    const waterMid = (sTarget + sStart) / 2;
    const [olo, ohi] = model.ranges.outdoor;
    for (let o = olo; o <= ohi + 1e-9; o += 0.5) {
      const rate = c0 + c1 * o + c2 * waterMid;
      if (rate > 0) modelPts.push([o, (sTarget - sStart) / rate]);
    }
    if (modelPts.length >= 2) modelNote = "linia: model grzania";
    else modelPts = [];
  }
  const W = 600, H = 200, L = 40, R = 8, T = 10, B = 24;
  const xs = pts.map(c => c.outdoor_c), ys = pts.map(c => c.duration_min);
  const xlo = Math.floor(Math.min(...xs)) - 1, xhi = Math.ceil(Math.max(...xs)) + 1;
  const yhi = Math.max(10, Math.ceil(Math.max(...ys, thr ?? 0, ...modelPts.map(p => p[1])) / 10) * 10);
  const x = v => L + ((v - xlo) * (W - L - R)) / (xhi - xlo);
  const y = v => T + ((yhi - v) * (H - T - B)) / yhi;
  const ystep = yhi > 60 ? 20 : 10, xstep = xhi - xlo > 20 ? 5 : 2;
  let grid = "";
  for (let v = 0; v <= yhi + 1e-9; v += ystep) grid += `<line class="pc-grid" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>` +
    `<text class="pc-tick" x="${L - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end">${v}</text>`;
  for (let v = Math.ceil(xlo / xstep) * xstep; v <= xhi; v += xstep) grid += `<text class="pc-tick" x="${x(v).toFixed(1)}" y="${H - 6}" text-anchor="middle">${v}°</text>`;
  const thrLine = thr === null ? "" : `<line class="pc-grid" style="stroke:var(--peak)" stroke-dasharray="6 4" x1="${L}" x2="${W - R}" ` +
    `y1="${y(thr).toFixed(1)}" y2="${y(thr).toFixed(1)}"/>`;
  const curve = modelPts.length ? `<polyline fill="none" style="stroke:var(--text)" stroke-width="2" points="${modelPts.map(([o, t]) =>
    `${x(o).toFixed(1)},${y(t).toFixed(1)}`).join(" ")}"/>` : "";
  const dots = pts.map(c => `<circle class="sc-dot${isAh(c) ? " sc-ah" : ""}" cx="${x(c.outdoor_c).toFixed(1)}" cy="${y(c.duration_min).toFixed(1)}" r="5">` +
    `<title>${esc(when(c.start))} · zewn. ${c.outdoor_c}°C · czas ${c.duration_min} min</title></circle>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Czas runu CWU wg temperatury zewnętrznej">${grid}${thrLine}${curve}${dots}</svg>` +
    `<figcaption><span class="sc-title">Czas runu CWU (min) wg temperatury zewnętrznej (°C)</span>` +
    `<span><i class="sc-keydot"></i>bez AH</span><span><i class="sc-keydot sc-ah"></i>z AH</span>` +
    (thr === null ? "" : `<span>próg slotu 52: ${thr} min</span>`) + `<span>${modelNote}</span></figcaption>`;
}

// Wykres 3: dopłata za zaoszczędzoną minutę na cykl z AH; linia = średnia z ah_effect.
function svgAhValue(a) {
  const rows = (a.rows || []).filter(r => hasVal(r.extra_pln) && hasVal(r.saved_min) && r.saved_min > 0);
  if (!rows.length) return "";
  const vals = rows.map(r => r.extra_pln / r.saved_min);
  const avg = hasVal(a.pln_per_saved_min) ? a.pln_per_saved_min : null;
  const W = 600, H = 190, L = 44, R = 8, T = 12, B = 30;
  const hi = Math.max(...vals, avg ?? 0, 0.01), lo = Math.min(...vals, 0);
  const y = v => T + ((hi - v) * (H - T - B)) / (hi - lo);
  const bw = (W - L - R) / rows.length;
  let body = `<text class="bar-label" x="${L - 4}" y="${T + 4}" text-anchor="end">${num(hi, 2)}</text>` +
    `<text class="bar-label" x="${L - 4}" y="${y(0).toFixed(1)}" text-anchor="end">0</text>` +
    `<line class="pc-grid" x1="${L}" x2="${W - R}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>`;
  vals.forEach((v, i) => {
    const top = Math.min(y(v), y(0)), h = Math.abs(y(v) - y(0));
    body += `<rect class="bar-fill" x="${(L + i * bw + bw * 0.12).toFixed(1)}" width="${(bw * 0.76).toFixed(1)}" y="${top.toFixed(1)}" ` +
      `height="${h.toFixed(1)}"><title>${esc(when(rows[i].start))} · ${num(v, 2)} zł/min</title></rect>`;
  });
  if (avg !== null) body += `<line style="stroke:var(--peak)" stroke-width="2" stroke-dasharray="6 4" x1="${L}" x2="${W - R}" ` +
    `y1="${y(avg).toFixed(1)}" y2="${y(avg).toFixed(1)}"/><text class="bar-label" x="${W - R}" y="${(y(avg) - 4).toFixed(1)}" text-anchor="end">średnio ${avg} zł/min</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Dopłata grzałki za zaoszczędzoną minutę, cykl po cyklu">${body}</svg>` +
    `<figcaption><span class="sc-title">Dopłata grzałki za zaoszczędzoną minutę, zł/min (cykl z AH)</span>` +
    (avg !== null ? `<span><i class="sc-keydot sc-ah"></i>średnia</span>` : "") + `</figcaption>`;
}

function renderDhwGroups(payload) {
  const groups = payload.groups || [];
  put("dhw-groups", table(["Okres", HPC.html(HPC.term("slot49", "Grzałka przy CWU")), HPC.html(HPC.term("slot50", "Priorytet grzałki")),
           HPC.html(HPC.term("slot52", "Opóźnienie grzałki (min)")), "Cykle ≥19 min", "Z grzałką", "Mediana minuty wejścia", "kWh grzałki", "zł grzałki", "Mała próba"],
      groups.map(g => [`${when(g.first)} – ${when(g.last)}`, cfgSwitch(g.cfg_49), g.cfg_50 ?? "?", g.cfg_52 ?? "?", g.cycles, g.cycles_with_ah,
        g.median_ah_tick_min, g.kwh, g.pln, g.small_sample ? "tak" : ""]).reverse(), { empty: "Brak cykli." }));
}

function renderDhwCyclesTable(payload) {
  const cycles = payload.cycles || [];
  put("dhw-cycles", `<h4>Cykle (najnowsze u góry)</h4>` +
    table(["Start", "Typ", "Cel °C", "Woda start", "Woda min 19", "Deficyt", "T zewn.", "Czas min",
           "Minuta wejścia grzałki", "Grzałka min", "Grzałka kWh", "Grzałka zł", "Grzałka przy CWU", "Priorytet", "Opóźnienie",
           "Koszt zł", "Udział PV", "Strefa"],
      cycles.map(c => [new Date(c.start).toLocaleString("pl-PL"), c.type, c.target_c, c.water_start_c,
        c.water_min19_c, c.deficit_c, c.outdoor_c, c.duration_min, c.ah_tick_min, c.ah_min, c.ah_kwh, c.ah_pln,
        cfgSwitch(c.cfg_49), c.cfg_50 ?? "?", c.cfg_52 ?? "?", c.cost_pln, pctTxt(c.pv_share), c.zone])));
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

function dhwVerdict(a) {
  if (!a.cycles_with_ah) return "W oknie 60 dni grzałka AH nie weszła ani razu — przy opóźnieniu jej startu 30 min to normalne.";
  if (!a.evaluated) return `Grzałka AH weszła ${a.cycles_with_ah} raz(y), ale żadnego cyklu nie da się jeszcze wycenić (powód w szczegółach poniżej).`;
  return `Grzałka AH weszła ${a.cycles_with_ah} raz(y) w 60 dni, dołożyła ${a.extra_kwh} kWh (${a.extra_pln} zł) i skróciła grzanie o ${a.saved_min} min` +
    `${a.pln_per_saved_min === null ? "" : ` — ${a.pln_per_saved_min} zł za zaoszczędzoną minutę`}. Nie opłaca się, jeśli liczy się koszt: ` +
    `grzałka daje 1 kWh ciepła z 1 kWh prądu, sprężarka ok. 3 kWh; ma sens tylko jako przyspieszenie.`;
}

// Przebieg wybranego cyklu: woda co minutę, odcinek z AH innym kolorem, przerywana „sama sprężarka” od wejścia AH.
function drawTrace(c, a) {
  const note = document.getElementById("dhw-trace-note");
  if (!c.trace || !c.trace.length) {
    HPC.chart(document.getElementById("dhw-trace"), { data: [], series: [], empty: "Ten cykl pochodzi sprzed zapisu przebiegów — nie ma minutowego wykresu." });
    note.textContent = ""; return;
  }
  const t0 = Date.parse(c.start) / 1000, tick = c.ah_tick_min;
  const cf = ((a.rows || []).find(r => r.start === c.start) || {}).counterfactual;
  const mins = new Set(c.trace.map(p => p[0]));
  if (cf) cf.forEach(p => mins.add(p[0]));
  const xs = [...mins].sort((p, q) => p - q), val = new Map(c.trace);
  const at = m => val.has(m) ? val.get(m) : null;
  const comp = xs.map(m => (tick == null || m <= Math.ceil(tick)) ? at(m) : null);
  const withAh = xs.map(m => (tick != null && m >= tick) ? at(m) : null);
  const line = xs.map(m => !cf || m < cf[0][0] || m > cf[1][0] ? null :
    cf[0][1] + (cf[1][1] - cf[0][1]) * (m - cf[0][0]) / ((cf[1][0] - cf[0][0]) || 1));
  HPC.chart(document.getElementById("dhw-trace"), {
    unit: "°C (czujnik na pompie)", height: 240, data: [xs.map(m => t0 + m * 60), comp, withAh, line],
    series: [{ label: "Grzeje sprężarka", color: "--accent" }, { label: "Dochodzi grzałka AH", color: "--peak" },
             { label: "Sama sprężarka (model)", color: "--muted", dash: [6, 4] }],
  });
  note.textContent = tick == null ? "W tym cyklu grzałka nie weszła." :
    cf ? `Grzałka weszła w ${tick}. minucie. Linia przerywana pokazuje, jak długo trwałoby to samo ogrzanie samą sprężarką.` :
      `Grzałka weszła w ${tick}. minucie; ten cykl nie ma wyceny (model niegotowy albo warunki poza jego zakresem).`;
}

function renderDhwTrace(payload) {
  const a = payload.ah_effect || {}, cycles = (payload.cycles || []).slice(0, 30);
  const pick = document.getElementById("dhw-pick");
  if (!cycles.length) { pick.innerHTML = ""; HPC.chart(document.getElementById("dhw-trace"), { data: [], series: [], empty: "Brak cykli CWU w dzienniku." }); return; }
  pick.innerHTML = cycles.map((c, i) => `<option value="${i}">${esc(when(c.start))}${c.ah_tick_min != null ? " — z grzałką" : ""}` +
    `${c.trace && c.trace.length ? "" : " (sprzed zapisu przebiegów)"}</option>`).join("");
  const best = cycles.findIndex(c => c.ah_tick_min != null && c.trace && c.trace.length);
  const first = best >= 0 ? best : Math.max(0, cycles.findIndex(c => c.trace && c.trace.length));
  pick.value = String(first);
  pick.onchange = () => drawTrace(cycles[Number(pick.value)], a);
  drawTrace(cycles[first], a);
}

// Sugestie Doradcy dla slotu 52 (opóźnienie grzałki AH): karta z zatwierdzeniem (zapis do pompy) i odrzuceniem.
const SUGGESTION_EVIDENCE = {
  pln_per_saved_min: "koszt AH za zaoszczędzoną minutę (zł/min)", ah_cycles: "cykli z grzałką AH", window_days: "okno (dni)",
  max_pln_per_min: "próg kosztu AH (zł/min)", winter_cycles: "cykli zimowych", over_share: "udział cykli dłuższych niż opóźnienie",
  median_deficit_c: "mediana deficytu wody w 19. min (°C)", outdoor_below_c: "zimą = zewn. poniżej (°C)",
};
const evTxt = v => v === true ? "tak" : v === false ? "nie" : hasVal(v) ? (typeof v === "number" ? num(v, 2) : String(v)) : "—";

function suggestionCard(p) {
  const evidence = Object.entries(p.evidence || {})
    .map(([k, v]) => `<li>${esc(SUGGESTION_EVIDENCE[k] || k)}: ${esc(evTxt(v))}</li>`).join("");
  return `<article class="proposal" data-id="${esc(p.id)}">
    <header><span class="badge">Doradca</span><span class="sub">pewność: ${esc(p.confidence)}</span></header>
    <div class="change"><strong>Opóźnienie grzałki (slot 52): z ${esc(num(p.from_value))} na ${esc(num(p.to_value))} min</strong></div>
    <p>${esc(p.reason)}</p>
    ${evidence ? `<details><summary>Na czym to opieram</summary><ul>${evidence}</ul></details>` : ""}
    <p class="sub">Zapis do pompy: slot 52, krok 5 min, potwierdzenie z ramki po kilku minutach.</p>
    <div class="actions">
      <button type="button" data-decision="zatwierdzona">Zatwierdź i zapisz w pompie</button>
      <button type="button" class="secondary" data-decision="odrzucona">Odrzuć</button>
      <span class="decision-status" role="status"></span>
    </div></article>`;
}

async function renderSuggestions() {
  const box = document.getElementById("cwu-suggestions");
  if (!box) return;
  try {
    const data = await HPC.getJSON("/api/proposals");
    const items = (data.pending || []).filter(p => p.analyzer === "dhw_slot");
    box.innerHTML = items.map(suggestionCard).join("");
    box.hidden = !items.length;
  } catch (e) { box.hidden = true; }
}

document.addEventListener("click", async ev => {
  const btn = ev.target.closest("#cwu-suggestions button[data-decision]");
  if (!btn) return;
  const cardEl = btn.closest(".proposal"), status = cardEl.querySelector(".decision-status");
  cardEl.querySelectorAll("button").forEach(b => { b.disabled = true; });
  status.textContent = "Zapisuję…";
  try {
    const resp = await fetch(`${HPC.base}/api/proposals/${cardEl.dataset.id}/decision`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: btn.dataset.decision }) });
    const result = await resp.json();
    if (result.ok) { await renderSuggestions(); return; }
    status.textContent = result.error || `Błąd HTTP ${resp.status}`;
    cardEl.querySelectorAll("button").forEach(b => { b.disabled = false; });
  } catch (e) {
    status.textContent = "Błąd zapisu decyzji.";
    cardEl.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});

function renderCwu(payload) {
  const all = payload.cycles || [], recent = recentCycles(all), a = payload.ah_effect || {};
  renderTiles(recent);
  put("chart-runs-hour", svgRunsByHour(recent, payload.hour_levels || []));
  put("chart-duration", svgDuration(recent, all, payload.model || {}));
  put("chart-ah-value", svgAhValue(a));
  put("ah-verdict", HPC.insight(dhwVerdict(a), "Werdykt:"));
  renderDhwGroups(payload);
  renderDhwCyclesTable(payload);
  renderDhwModel(payload);
  renderDhwTrace(payload);
  renderSuggestions();
}

async function loadCwu() {
  try { renderCwu(await HPC.getJSON("/api/dhw_cycles")); }
  catch (e) { put("dhw-groups", "Błąd wczytywania cykli CWU."); }
}
loadCwu();
