// Wspólne elementy UI: słowniczek z dymkami, wniosek „co z tego wynika”, zwijane tabele, wykresy (uPlot).
// Ładowane po app.js (rozszerza HPC) i glossary.js.
(() => {
const esc = s => String(s ?? "—").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const html = s => ({ html: s });                       // znacznik „to już bezpieczny HTML” dla nagłówków i komórek
const cell = c => (c && typeof c === "object" && "html" in c) ? c.html : esc(c);

// Termin ze słowniczka: kropkowane podkreślenie, objaśnienie po najechaniu/dotknięciu (title nie działa w WebView).
HPC.term = (key, label) => {
  const t = GLOSSARY[key];
  if (!t) return esc(label || key);
  return `<abbr class="term" tabindex="0" data-tip="${esc(t[1])}">${esc(label || t[0])}</abbr>`;
};

HPC.insight = (text, label = "Co z tego wynika:") => `<p class="insight">${label ? `<strong>${esc(label)}</strong> ` : ""}${esc(text)}</p>`;

// Tabela z najnowszymi `show` wierszami i przyciskiem „Pokaż wszystkie (N)”. Wiersze podaj od najnowszego.
// Nagłówki i komórki: tekst (escapowany) albo html("…").
HPC.collapsibleTable = (head, rows, { show = 5, empty = "Brak danych." } = {}) => {
  if (!rows.length) return `<p class="sub">${esc(empty)}</p>`;
  const fmt = v => (typeof v === "number" && !Number.isInteger(v)) ? v.toFixed(2) : v;
  const body = rows.map((r, i) => `<tr${i >= show ? " hidden data-extra" : ""}>${r.map(c =>
    `<td>${cell(c && typeof c === "object" ? c : fmt(c))}</td>`).join("")}</tr>`).join("");
  const more = rows.length > show
    ? `<button type="button" class="table-more" data-total="${rows.length}" data-shown="${show}" aria-expanded="false">Pokaż wszystkie (${rows.length})</button>` : "";
  return `<div class="ctable"><table><thead><tr>${head.map(h => `<th>${cell(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>${more}</div>`;
};

document.addEventListener("click", ev => {
  const btn = ev.target.closest("button.table-more");
  if (!btn) return;
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.closest(".ctable").querySelectorAll("tr[data-extra]").forEach(tr => { tr.hidden = open; });
  btn.setAttribute("aria-expanded", String(!open));
  btn.textContent = open ? `Pokaż wszystkie (${btn.dataset.total})` : `Pokaż tylko ${btn.dataset.shown} ostatnich`;
});

// ── Wykresy ──────────────────────────────────────────────────────────────────────────────────────────────
const WAW = { timeZone: "Europe/Warsaw", hour12: false };
const fmtDay = new Intl.DateTimeFormat("pl-PL", { ...WAW, day: "2-digit", month: "2-digit" });
const fmtHour = new Intl.DateTimeFormat("pl-PL", { ...WAW, hour: "2-digit", minute: "2-digit" });
const fmtFull = new Intl.DateTimeFormat("pl-PL", { ...WAW, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
// Oś czasu: do 2 dób sama godzina (HH:MM); dłużej przy kroku co najmniej doba sama data (DD.MM), a przy krótszym kroku
// data tylko o północy, w pozostałych punktach godzina.
const timeTicks = (u, splits) => {
  const span = splits[splits.length - 1] - splits[0], step = splits.length > 1 ? splits[1] - splits[0] : span;
  return splits.map(v => {
    const d = new Date(v * 1000), hour = fmtHour.format(d);
    if (span <= 2 * 86400) return hour;
    return (step >= 86400 || hour === "00:00") ? fmtDay.format(d) : hour;
  });
};
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const charts = [];

// opts: { unit, unit2, series: [{ label, color: "--accent", kind: "line"|"bars"|"step", dash, width, y2, fill, shown }], data: [xs(sek), ...ys], height, yRange, empty }
// `shown` = tablica wartości do legendy zamiast wartości serii (słupki układane jedna na drugiej).
// Zwraca uchwyt; null, gdy brak danych (wtedy w kontenerze pojawia się wyjaśnienie).
const byEl = new Map();
// bands: [{ from, to, color }] — pasy w obszarze wykresu wg skali y; null = brzeg osi. Kolor: zmienna CSS ("--x") albo css.
const drawBands = (u, bands) => {
  if (!bands) return;
  const { left, top, width, height } = u.bbox, bottom = top + height;
  const pos = v => Math.min(bottom, Math.max(top, u.valToPos(v, "y", true)));
  u.ctx.save();
  bands.forEach(b => {
    const y1 = b.from == null ? bottom : pos(b.from), y2 = b.to == null ? top : pos(b.to);
    u.ctx.fillStyle = b.color.startsWith("--") ? cssVar(b.color) : b.color;
    u.ctx.fillRect(left, Math.min(y1, y2), width, Math.abs(y1 - y2));
  });
  u.ctx.restore();
};
HPC.chart = (el, opts) => {
  if (byEl.has(el)) byEl.get(el).destroy();                // nowy wykres w tym samym miejscu zastępuje stary
  if (typeof uPlot === "undefined" || !opts.data || !opts.data[0] || opts.data[0].length < 2) {
    el.innerHTML = `<p class="sub">${esc(opts.empty || "Za mało danych na wykres — zbierają się z kolejnych godzin.")}</p>`;
    return null;
  }
  const build = () => {
    el.innerHTML = "";
    const text = cssVar("--muted"), grid = cssVar("--line");
    const hasY2 = opts.series.some(s => s.y2);
    const series = [{ label: "Moment", value: (u, v) => v == null ? "—" : fmtFull.format(new Date(v * 1000)) }, ...opts.series.map(s => {
      const color = cssVar(s.color || "--accent");
      const out = { label: s.label, stroke: color, width: s.kind === "bars" ? 0 : (s.width || 2), scale: s.y2 ? "y2" : "y",
        spanGaps: false, points: { show: false } };
      if (s.dash) out.dash = s.dash;
      if (s.kind === "bars") { out.fill = color + "cc"; out.paths = uPlot.paths.bars({ size: [0.8, 40] }); }
      else if (s.kind === "step") out.paths = uPlot.paths.stepped({ align: 1 });
      if (s.fill) out.fill = s.gradient
        ? u => { const b = u.bbox;
            if (!b || !Number.isFinite(b.top + b.height)) return color + "59";   // uPlot woła fill też dla znacznika legendy, zanim policzy obszar wykresu
            const g = u.ctx.createLinearGradient(0, b.top, 0, b.top + b.height);   // pionowy: 35% krycia u góry, 0% na dole
            g.addColorStop(0, color + "59"); g.addColorStop(1, color + "00"); return g; }
        : color + "22";
      if (s.shown) out.value = (u, v, si, i) => (i == null || s.shown[i] == null) ? "—" : String(Math.round(s.shown[i] * 100) / 100);
      return out;
    })];
    const axis = (scale, label, side) => ({ scale, side, label, labelSize: 14, stroke: text, size: 46,
      grid: scale === "y" ? { stroke: grid, width: 1 } : { show: false }, ticks: { stroke: grid }, font: "11px system-ui", labelFont: "11px system-ui" });
    const plot = new uPlot({
      width: el.clientWidth || 600, height: opts.height || 220,
      cursor: { drag: { x: false, y: false } }, legend: { live: true },
      scales: { x: { time: true }, y: { range: opts.yRange || undefined }, ...(hasY2 ? { y2: {} } : {}) },
      hooks: { drawClear: [u => drawBands(u, opts.bands)] },
      axes: [{ values: timeTicks, stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: "11px system-ui" },
        axis("y", opts.unit || "", 3), ...(hasY2 ? [axis("y2", opts.unit2 || "", 1)] : [])],
      series,
    }, opts.data, el);
    return plot;
  };
  const observer = new ResizeObserver(() => handle.plot.setSize({ width: el.clientWidth, height: opts.height || 220 }));
  const handle = { plot: build(), rebuild() { this.plot.destroy(); this.plot = build(); },
    destroy() { observer.disconnect(); this.plot.destroy(); charts.splice(charts.indexOf(this), 1); byEl.delete(el); } };
  charts.push(handle); byEl.set(el, handle);
  observer.observe(el);
  return handle;
};
// Motyw jasny/ciemny zmienia kolory zmiennych CSS — wykresy trzeba zbudować od nowa.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => charts.forEach(c => c.rebuild()));

  HPC.esc = esc; HPC.html = html;
  HPC.put = (id, markup) => { const el = document.getElementById(id); if (el) el.innerHTML = markup; };
})();
