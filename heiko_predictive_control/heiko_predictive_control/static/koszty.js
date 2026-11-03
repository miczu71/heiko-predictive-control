// Zakładka „Koszty”: GET /api/costs (K2) — sumy, Δ vs poprzedni okres, wykres skumulowany, tabela stref. Tylko odczyt.
(() => {
const esc = HPC.esc, nf = HPC.nf, pct = HPC.pct;
const $ = id => document.getElementById(id);
const KEY = "hpc_koszty";
const COMP = [["floor", "Podłoga"], ["dhw", "Ciepła woda"], ["ac", "Klimatyzacja"]];
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const plDate = key => new Date(`${key}T12:00:00`).toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit", year: "numeric" });

const state = { from: "", to: "", group: "auto", preset: "30", metric: "pln" };
try { Object.assign(state, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) { /* tryb prywatny */ }
if (!["pln", "kwh"].includes(state.metric)) state.metric = "pln";
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* zakres nie zostanie zapamiętany */ } };

function presetRange(p) {
  const today = new Date(), y = today.getFullYear(), m = today.getMonth();
  if (p === "7") return [addDays(today, -6), today];
  if (p === "30") return [addDays(today, -29), today];
  if (p === "month") return [new Date(y, m, 1), today];
  if (p === "prev-month") return [new Date(y, m - 1, 1), new Date(y, m, 0)];
  if (p === "season") return [new Date(m >= 9 ? y : y - 1, 9, 1), today];
  return null;
}

// Klucz koszyka z backendu to zawsze data ISO (doba, poniedziałek tygodnia albo 1. dzień miesiąca) → południe tego dnia (s).
const bucketTime = key => new Date(`${key}T12:00:00`).getTime() / 1000;

const sign = v => v > 0 ? "+" : v < 0 ? "−" : "";
const delta = (v, unit, d = 2) => v == null ? "" : `${sign(v)}${nf(Math.abs(v), d)} ${unit}`;
const tile = (label, value, sub) => `<div class="cwu-tile"><span class="cwu-tile-value">${value}</span><span class="cwu-tile-label">${esc(label)}</span>${sub ? `<span class="cwu-tile-delta">${sub}</span>` : ""}</div>`;
const approxMark = c => c && c.approx ? `<abbr class="approx" title="Wartość przybliżona: brak części danych o źródle energii lub run w toku">≈</abbr> ` : "";

function renderKpi(d) {
  const t = d.totals.total, dl = d.delta && d.delta.total;
  const cmp = d.previous ? `vs ${plDate(d.previous.from)}–${plDate(d.previous.to)}` : "brak okresu porównawczego";
  $("k-kpi").innerHTML = [
    tile("Koszt razem", `${approxMark(t)}${nf(t.pln)} zł`, dl ? `${delta(dl.pln, "zł")} ${cmp}` : cmp),
    tile("Energia razem", `${nf(t.kwh)} kWh`, dl ? delta(dl.kwh, "kWh") : ""),
    tile("Z własnego PV", pct(t.pv_share), dl && dl.pv_share_pp != null ? delta(dl.pv_share_pp, "p.p.", 1) : ""),
  ].join("");
  $("k-comp").innerHTML = `<thead><tr><th></th><th>Energia</th><th>Koszt</th><th>Δ koszt</th><th>Z PV</th></tr></thead><tbody>`
    + COMP.map(([k, l]) => {
      const c = d.totals[k], dd = d.delta && d.delta[k];
      return `<tr><th scope="row">${l}</th><td>${approxMark(c)}${nf(c.kwh)} kWh</td><td>${nf(c.pln)} zł</td><td class="delta">${dd ? delta(dd.pln, "zł") : "—"}</td><td>${pct(c.pv_share)}</td></tr>`;
    }).join("") + `</tbody>`;
}

function renderChart(d) {
  const m = state.metric, b = d.buckets;
  const val = (x, k) => (x[k] && x[k][m]) || 0;
  const t = b.map(x => bucketTime(x.key));
  const f = b.map(x => val(x, "floor")), w = b.map(x => val(x, "dhw")), a = b.map(x => val(x, "ac"));
  const fw = f.map((v, i) => v + w[i]), all = fw.map((v, i) => v + a[i]);
  const unit = m === "pln" ? "zł" : "kWh";
  // Słupki skumulowane: nakładane od najwyższego (suma) do najniższego (podłoga); legenda pokazuje wartości składników.
  HPC.chart($("k-chart"), { unit, unit2: "°C na zewnątrz", empty: "Za mało koszyków na wykres — wybierz zakres co najmniej dwóch dni.",
    yRange: (u, lo, hi) => [0, Math.max(hi, 0.1)],
    data: [t, all, fw, f, b.map(x => x.outdoor_c)],
    series: [{ label: "Klimatyzacja", color: "--chart-out", kind: "bars", shown: a }, { label: "Ciepła woda", color: "--peak", kind: "bars", shown: w },
      { label: "Podłoga", color: "--chart-avg", kind: "bars", shown: f }, { label: "Na zewnątrz", color: "--muted", y2: true, width: 1.5 }] });
}

function renderZones(d) {
  const rows = [...COMP, ["total"]];
  const names = [...new Set(rows.flatMap(([k]) => Object.keys(d.totals[k].sources || {})))]
    .sort((x, y) => HPC.originRank(x) - HPC.originRank(y));
  const showRest = rows.some(([k]) => (d.totals[k].unsplit_kwh || 0) > 0.005);
  const cell = (x) => x && x.kwh > 0
    ? `${nf(x.kwh)} kWh · ${nf(x.pln)} zł${x.battery_kwh > 0.005 ? `<br><span class="muted">${nf(x.battery_kwh)} przez baterię</span>` : ""}` : "—";
  const head = `<thead><tr><th></th>${names.map(n => `<th>${esc(HPC.originLabel(n))}</th>`).join("")}${showRest ? "<th>bez podziału</th>" : ""}<th>Z PV</th></tr></thead>`;
  const span = names.length + (showRest ? 1 : 0) + 2;
  const row = (k, l, total) => {
    const c = d.totals[k];
    return `<tr${total ? ' class="total"' : ""}><th scope="row">${l}</th>${names.map(n => `<td>${cell((c.sources || {})[n])}</td>`).join("")}`
      + `${showRest ? `<td>${c.unsplit_kwh > 0.005 ? nf(c.unsplit_kwh) + " kWh" : "—"}</td>` : ""}<td>${pct(c.pv_share)}</td></tr>`
      + (c.kwh > 0 ? `<tr class="zones-row"><td colspan="${span}">${HPC.sourceBar(c.sources, c.kwh)}</td></tr>` : "");
  };
  $("k-zones").innerHTML = head + `<tbody>${COMP.map(([k, l]) => row(k, l)).join("")}${row("total", "Razem", true)}</tbody>`;
}

function renderNote(d) {
  const c = d.coverage || {}, parts = [];
  if (c.floor && c.floor.oldest) parts.push(`podłogówka od ${plDate(c.floor.oldest)}`);
  if (c.dhw && c.dhw.oldest) parts.push(`CWU od ${plDate(c.dhw.oldest)}${c.dhw.sources_from ? `, pochodzenie energii CWU od ${plDate(c.dhw.sources_from)}` : ", bez pochodzenia energii"}`);
  if (c.ac && c.ac.oldest) parts.push(`klimatyzacja od ${plDate(c.ac.oldest)}`);
  const empty = d.totals.total.kwh <= 0 ? " W wybranym zakresie nie ma zapisanych rekordów." : "";
  $("k-note").textContent = (parts.length ? `Zakres danych: ${parts.join("; ")}. ` : "Brak zapisanych rekordów. ") + `Koszyki: ${({ day: "doby", week: "tygodnie", month: "miesiące" })[d.group] || d.group}.${empty}`;
}

let seq = 0;
async function load() {
  const my = ++seq;
  document.querySelectorAll("#k-presets button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.preset === state.preset)));
  document.querySelectorAll("#k-metric button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.metric === state.metric)));
  $("k-from").value = state.from; $("k-to").value = state.to; $("k-group").value = state.group;
  const err = $("k-error");
  try {
    const resp = await fetch(`${HPC.base}/api/costs?from=${state.from}&to=${state.to}&group=${state.group}`, { headers: { Accept: "application/json" } });
    const d = await resp.json();
    if (my !== seq) return;
    if (!resp.ok) { clearView(); err.textContent = d.error || `Błąd ${resp.status}`; err.hidden = false; return; }
    err.hidden = true; window.__costs = d;
    renderKpi(d); renderChart(d); renderZones(d); renderNote(d);
  } catch (e) {
    console.error(e);
    if (my === seq) { clearView(); err.textContent = "Nie udało się wczytać kosztów."; err.hidden = false; }
  }
}

// Po błędzie nie zostawiamy liczb z poprzedniego zakresu obok komunikatu.
function clearView() { ["k-kpi", "k-chart", "k-zones"].forEach(id => { $(id).innerHTML = ""; }); }

function applyPreset(p) {
  const r = presetRange(p);
  state.preset = p; state.from = iso(r[0]); state.to = iso(r[1]); save(); load();
}

$("k-presets").addEventListener("click", ev => { const b = ev.target.closest("button[data-preset]"); if (b) applyPreset(b.dataset.preset); });
$("k-metric").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-metric]"); if (!b) return;
  state.metric = b.dataset.metric; save();
  document.querySelectorAll("#k-metric button").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.metric === state.metric)));
  if (window.__costs) renderChart(window.__costs);
});
const custom = () => { state.preset = ""; state.from = $("k-from").value; state.to = $("k-to").value; state.group = $("k-group").value; save(); load(); };
["k-from", "k-to", "k-group"].forEach(id => $(id).addEventListener("change", custom));
$("k-to").max = iso(new Date());

// Preset liczymy od nowa (zapisane daty po dniach są nieaktualne); zapisane od–do tylko dla zakresu własnego.
if (presetRange(state.preset)) applyPreset(state.preset);
else if (!/^\d{4}-\d{2}-\d{2}$/.test(state.from) || !/^\d{4}-\d{2}-\d{2}$/.test(state.to)) applyPreset("30");
else load();
})();
