// /ogrzewanie: runy podłogówki (GET /api/heating_runs) — doba → strefy taryfy → pojedyncze runy. Tylko odczyt.
(() => {
const esc = HPC.esc, nf = HPC.nf, pct = HPC.pct;
const TZ = { timeZone: "Europe/Warsaw" };
const dayKey = iso => new Date(iso).toLocaleDateString("sv-SE", TZ);                        // RRRR-MM-DD wg czasu lokalnego
const dayLabel = key => new Date(`${key}T12:00:00`).toLocaleDateString("pl-PL", { weekday: "short", day: "2-digit", month: "2-digit" });
const hhmm = iso => new Date(iso).toLocaleTimeString("pl-PL", { ...TZ, hour: "2-digit", minute: "2-digit" });
const sum = (xs, f) => xs.reduce((a, x) => a + (f(x) || 0), 0);
const dur = m => m == null ? "—" : (m >= 60 ? `${Math.floor(m / 60)} h ${Math.round(m % 60)} min` : `${Math.round(m)} min`);

const plural = n => n === 1 ? "run" : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) ? "runy" : "runów";

function group(runs) {
  const days = new Map();
  for (const r of runs) { const k = dayKey(r.start); (days.get(k) || days.set(k, []).get(k)).push(r); }
  return [...days.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([key, rs]) => {
    const sources = {};
    for (const r of rs) for (const [n, x] of Object.entries(r.sources || {})) {
      const a = sources[n] || (sources[n] = { kwh: 0, pln: 0, battery_kwh: 0 });
      a.kwh += x.kwh || 0; a.pln += x.pln || 0; a.battery_kwh += x.battery_kwh || 0;
    }
    const known = rs.filter(r => r.split_complete && r.pv_share != null);
    const knownKwh = sum(known, r => r.energy_kwh);
    return { key, runs: rs.slice().sort((a, b) => a.start.localeCompare(b.start)), sources,
      kwh: sum(rs, r => r.energy_kwh), pln: sum(rs, r => r.cost_pln), minutes: sum(rs, r => r.duration_min),
      pv: knownKwh > 0 ? sum(known, r => r.energy_kwh * r.pv_share) / knownKwh : null,
      approx: rs.some(r => !r.split_complete || r.complete === false || r.cost_pln == null) };
  });
}

function runRow(r) {
  const flag = r.complete === false ? `<br><span class="badge">niekompletny</span>` : "";
  const pv = (r.split_complete || r.pv_share == null) ? pct(r.pv_share) : "≈ " + pct(r.pv_share);
  return `<tr><td>${hhmm(r.start)}–${r.end ? hhmm(r.end) : "…"}${flag}</td><td>${dur(r.duration_min)}</td><td>${nf(r.energy_kwh)}</td><td>${nf(r.cost_pln)}</td><td>${pv}</td><td class="opt">${nf(r.outdoor_c, 1)}</td><td class="opt">${nf(r.water_start_c, 1)}→${nf(r.water_end_c, 1)}</td></tr>`;
}

function dayBlock(d, open) {
  const approx = d.approx ? `<abbr class="approx" title="Wartość przybliżona: brak części danych o źródle energii lub run w toku">≈</abbr> ` : "";
  return `<details class="heat-day"${open ? " open" : ""}><summary><span class="hd-date">${esc(dayLabel(d.key))}</span>`
    + `<span>${approx}${nf(d.kwh)} kWh</span><span>${nf(d.pln)} zł</span><span>PV ${pct(d.pv)}</span><span class="muted">${d.runs.length} ${plural(d.runs.length)} · ${dur(d.minutes)}</span></summary>`
    + `<div class="hd-body">${HPC.sourceBar(d.sources, d.kwh)}<div class="ctable"><table><thead><tr><th>Run</th><th>Czas</th><th>kWh</th><th>zł</th><th>PV</th><th class="opt">T zewn. °C</th><th class="opt">T wody °C</th></tr></thead><tbody>`
    + d.runs.map(runRow).join("") + `</tbody></table></div></div></details>`;
}

function render(runs) {
  const days = group(runs);
  if (!days.length) {
    HPC.put("heat-summary", `<p class="sub">Brak zapisanych runów. Dziennik zapisuje się nocą o 00:30 i po starcie add-onu (7 dni wstecz); wymaga ustawionej encji energii pompy.</p>`);
    HPC.put("heat-days", "");
    return;
  }
  const kwh = sum(days, d => d.kwh), pln = sum(days, d => d.pln);
  const known = days.filter(d => d.pv != null), kk = sum(known, d => d.kwh);
  const pv = kk > 0 ? sum(known, d => d.kwh * d.pv) / kk : null;
  HPC.put("heat-summary", `<div class="stat-strip"><div class="stat"><span class="stat-label">Doby z grzaniem</span><span class="stat-value">${days.length}</span><span class="stat-sub">${runs.length} runów</span></div>`
    + `<div class="stat"><span class="stat-label">Energia / koszt</span><span class="stat-value">${nf(kwh, 1)} kWh</span><span class="stat-sub">${nf(pln)} zł w oknie</span></div>`
    + `<div class="stat"><span class="stat-label">Z własnego PV</span><span class="stat-value">${pct(pv)}</span><span class="stat-sub">udział w energii z pełnym podziałem</span></div></div>`);
  HPC.put("heat-days", days.map((d, i) => dayBlock(d, i === 0)).join(""));
}

HPC.getJSON("/api/heating_runs?days=60").then(d => render(d.runs || []))
  .catch((e) => { console.error(e); HPC.put("heat-summary", `<p class="notice-inline">Nie udało się wczytać runów podłogówki.</p>`); });
})();
