// Wspólne narzędzia — fetch zawsze względny/pod APP_BASE (ingress-safe).
const HPC = {
  base: window.APP_BASE || "",
  async getJSON(path) {
    const resp = await fetch(`${HPC.base}${path}`, { headers: { Accept: "application/json" } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  },
  async postJSON(path, body) {
    const resp = await fetch(`${HPC.base}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  },
};

// Strefy taryfy (feed pv_roi: cheap/mid/expensive; zapas G12w: offpeak/peak) i formatowanie liczb dla kafla doby i /ogrzewanie.
HPC.ZONE_LABEL = { cheap: "tania", mid: "średnia", expensive: "droga", offpeak: "poza szczytem", peak: "szczyt", other: "poza szczytem", morning_peak: "szczyt poranny", evening_peak: "szczyt popołudniowy" };
HPC.ZONE_ORDER = ["cheap", "offpeak", "other", "mid", "morning_peak", "peak", "evening_peak", "expensive"];
HPC.ZONE_CLASS = { cheap: "zone-cheap", offpeak: "zone-cheap", other: "zone-cheap", mid: "zone-mid", morning_peak: "zone-mid", peak: "zone-expensive", expensive: "zone-expensive", evening_peak: "zone-expensive" };
HPC.zoneLabel = z => HPC.ZONE_LABEL[z] || z;
HPC.zoneClass = z => HPC.ZONE_CLASS[z] || "zone-other";
HPC.nf = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d).replace(".", ",");
HPC.pct = v => (v === null || v === undefined) ? "—" : `${Math.round(v * 100)}%`;
// Pochodzenie energii: `grid:<strefa>` (sieć wg strefy taryfy z chwili ładowania/poboru), `pv`, `unknown` (zawartość baterii sprzed replayu).
// Kolejność: strefy sieci wg ZONE_ORDER, potem PV, potem nieznane.
HPC.originLabel = o => o === "pv" ? "PV" : o === "unknown" ? "nieznane" : o.startsWith("grid:") ? `sieć ${HPC.zoneLabel(o.slice(5))}` : o;
HPC.originClass = o => o === "pv" ? "src-pv" : o === "unknown" ? "src-unknown" : o.startsWith("grid:") ? HPC.zoneClass(o.slice(5)) : "zone-other";
HPC.originRank = o => o === "pv" ? 100 : o === "unknown" ? 101 : o.startsWith("grid:") ? (HPC.ZONE_ORDER.indexOf(o.slice(5)) + 1 || 50) : 102;
HPC.sourceEntries = sources => Object.entries(sources || {}).sort((a, b) => HPC.originRank(a[0]) - HPC.originRank(b[0]));
// Pasek pochodzenia energii: segmenty szerokością ∝ kWh, opis tekstem pod paskiem (nie tylko kolor).
// `totalKwh`: energia bez podziału na pochodzenie (rekordy sprzed `sources`) trafia do szarego segmentu „bez podziału”.
HPC.sourceBar = (sources, totalKwh) => {
  const rows = HPC.sourceEntries(sources).filter(([, x]) => x.kwh > 0)
    .map(([n, x]) => ({ n, label: HPC.originLabel(n), cls: HPC.originClass(n), kwh: x.kwh, pln: x.pln, bat: x.battery_kwh || 0 }));
  const rest = totalKwh > 0 ? totalKwh - rows.reduce((a, r) => a + r.kwh, 0) : 0;
  if (rest > 0.005) rows.push({ n: "rest", label: "bez podziału", cls: "zone-other", kwh: rest, pln: null, bat: 0 });
  const total = rows.reduce((a, r) => a + r.kwh, 0);
  if (!total) return "";
  const esc = HPC.esc;
  const seg = rows.map(r => `<i class="${r.cls}" style="flex:${r.kwh / total}" title="${esc(r.label)}: ${HPC.nf(r.kwh)} kWh"></i>`).join("");
  const legend = rows.map(r => `<span><i class="sw ${r.cls}"></i>${esc(r.label)} ${HPC.nf(r.kwh)} kWh${r.pln === null ? "" : ` · ${HPC.nf(r.pln)} zł`}${r.bat > 0.005 ? ` <span class="via-bat">(${HPC.nf(r.bat)} przez baterię)</span>` : ""}</span>`).join("");
  return `<div class="zonebar" role="img" aria-label="Pochodzenie energii">${seg}</div><p class="zonelegend">${legend}</p>`;
};

