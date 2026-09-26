// Raport doradcy D1 — wyłącznie odczyt. Wszystkie fetch-e względne (ingress).
const esc = s => String(s ?? "—").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmt = v => (v === null || v === undefined) ? "—" : (typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v);

function table(head, rows) {
  if (!rows.length) return "Brak danych.";
  return `<table><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map(r => `<tr>${r.map(c => `<td>${esc(fmt(c))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
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
    `<h3>Hipotezy (materiał do D2, nie propozycje)</h3><ul>${(r.ideas || []).map(i => `<li>${esc(i)}</li>`).join("") || "<li>brak</li>"}</ul>` +
    `<h3>Luki w danych</h3><ul>${(r.data_gaps || []).map(i => `<li>${esc(i)}</li>`).join("")}</ul>`);
  const c = r.comfort;
  put("report-comfort", !c ? "Brak danych." :
    `<p class="sub">Cel ${c.target}°C, pasmo dzień ±${c.band_day} / noc ±${c.band_night}, minimum pokoju ${c.room_min}°C · ${c.hours} godz. · ` +
    `najzimniejszy pokój: mediana ${fmt(c.coldest.p50)}, p5 ${fmt(c.coldest.p5)}, min ${fmt(c.coldest.min)}, godz. poniżej minimum: ${c.coldest.below_room_min_h}</p>` +
    table(["Pokój", "Godz.", "Średnia", "Od celu", "SD", "p5", "Mediana", "p95", "Min", "W paśmie"],
      [["ŚREDNIA STREF (to reguluje add-on)", c.hours, c.average.mean, c.average.offset, c.average.sd, null, null, null, null, `${(c.average.in_band * 100).toFixed(0)}%`],
       ...Object.entries(c.rooms).map(([k, v]) => [k, v.hours, v.mean, v.offset, v.sd, v.p5, v.p50, v.p95, v.min, v.in_band === null ? null : `${(v.in_band * 100).toFixed(0)}%`])]));
  const e = r.energy;
  put("report-energy", !e ? "Brak danych." :
    table(["Miesiąc", "kWh", "Stopniodni", "kWh/stopniodzień", "Godz."],
      Object.entries(e.months).map(([k, v]) => [k, v.kwh, v.hdd, v.kwh_per_hdd, v.hours])));
  put("report-peak-class", !e || !e.peak_share ? "" :
    `<p class="sub">Udział szczytu ogółem: ${(e.peak_share.overall * 100).toFixed(1)}% (${e.peak_share.kwh} kWh, ${e.peak_share.hours} godz.)</p>` +
    table(["Klasa temp. zewn. °C", "Udział szczytu", "kWh", "Godz."],
      byTemp(e.peak_share.by_class).map(([k, v]) => [k, v.share === null ? null : `${(v.share * 100).toFixed(0)}%`, v.kwh, v.hours])));
  const h = r.pump_hours;
  put("report-hours", !h ? "Brak danych." :
    table(["Klasa temp. zewn. °C", "Dni", "Grzanie h/dobę", "CWU h/dobę", "Postój h/dobę"],
      byTemp(h.by_class).map(([k, v]) => [k, v.days, v.heating_h, v.dhw_h, v.standby_h])));
  put("report-cop", !r.cop ? "Brak danych (latem pompa robi głównie CWU)." :
    table(["Klasa temp. zewn. °C", "Godz.", "Średni COP", "Min", "Max"],
      byTemp(r.cop).map(([k, v]) => [k, v.hours, v.mean, v.min, v.max])));
  const o = r.own || {}, a = o.avg_per_day || {};
  put("report-own", `<p class="sub">Dób ze streszczeniami: ${o.days ?? 0}</p>` +
    table(["Starty sprężarki", "Praca sprężarki min", "Krótkie cykle", "Śr. cykl min", "Cykle CWU", "CWU min", "Grzanie min", "AH min", "HBH min", "HWTBH min", "Impulsy P0", "kWh", "Udział szczytu"],
      [[a.compressor_starts, a.compressor_run_min, a.short_cycles, a.mean_run_min, a.dhw_cycles, a.dhw_min, a.heating_min, a.ah_min, a.hbh_min, a.hwtbh_min, a.p0_pulses, a.kwh, a.peak_share]]));
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

document.getElementById("report-refresh")?.addEventListener("click", async () => {
  await loadReport(true);
  const timer = setInterval(async () => {
    const p = await HPC.getJSON("/api/report");
    renderReport(p);
    if (!p.running) clearInterval(timer);
  }, 10000);
});
loadReport(false); loadChanges(); loadCatalog();
