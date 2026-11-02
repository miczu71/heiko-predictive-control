// Kafel „Doba” na pulpicie: koszt, strefy taryfy i udział PV dla podłogówki, CWU i klimatyzacji (GET /api/day_summary).
(() => {
const body = document.getElementById("day-body");
const tile = document.getElementById("day-tile");
if (!body || !tile) return;
const esc = HPC.esc, nf = HPC.nf;
const ROWS = [["floor", "Podłoga"], ["dhw", "Ciepła woda"], ["ac", "Klimatyzacja"]];
let current = "today";

function row(label, d, total) {
  if (d.configured === false) return `<tr><th scope="row">${esc(label)}</th><td colspan="3" class="muted">nie skonfigurowano</td></tr>`;
  const approx = d.approx ? `<abbr class="approx" title="Wartość przybliżona: brak części danych o źródle energii lub run w toku">≈</abbr> ` : "";
  const bar = d.kwh > 0 ? `<tr class="zones-row"><td colspan="4">${HPC.zoneBarWithRest(d.zones, d.kwh)}</td></tr>` : "";
  return `<tr${total ? ' class="total"' : ""}><th scope="row">${esc(label)}</th><td>${approx}${nf(d.kwh)} kWh</td><td>${nf(d.pln)} zł</td><td>${HPC.pct(d.pv_share)}</td></tr>${bar}`;
}

function render(d) {
  const notice = d.error ? `<p class="notice-inline">${esc(d.error)} — wartości mogą być niepełne.</p>` : "";
  const live = d.in_progress ? `<p class="sub">Doba trwa — runy w toku liczone do tej chwili.</p>` : "";
  const rows = ROWS.map(([k, l]) => row(l, d[k] || { configured: false })).join("") + row("Razem", d.total, true);
  body.innerHTML = `${notice}<table class="day-table"><thead><tr><th></th><th>Energia</th><th>Koszt</th><th>Z PV</th></tr></thead><tbody>${rows}</tbody></table>`
    + (d.total && d.total.kwh > 0 ? "" : `<p class="sub">Brak zużycia w tej dobie.</p>`) + live;
}

async function load(day) {
  current = day;
  tile.querySelectorAll("[data-day]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.day === day)));
  body.innerHTML = "ładowanie…";
  try { const d = await HPC.getJSON(`/api/day_summary?day=${day}`); if (day === current) render(d); }
  catch (e) { console.error(e); if (day === current) body.innerHTML = `<p class="notice-inline">Nie udało się wczytać podsumowania doby.</p>`; }
}

tile.addEventListener("click", ev => { const b = ev.target.closest("[data-day]"); if (b) load(b.dataset.day); });
load(current);
setInterval(() => { if (current === "today" && !document.hidden) load("today"); }, 300000);
})();
