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
HPC.ZONE_LABEL = { cheap: "tania", mid: "średnia", expensive: "droga", offpeak: "poza szczytem", peak: "szczyt" };
HPC.ZONE_ORDER = ["cheap", "offpeak", "mid", "peak", "expensive"];
HPC.zoneLabel = z => HPC.ZONE_LABEL[z] || z;
HPC.zoneClass = z => HPC.ZONE_LABEL[z] ? `zone-${z}` : "zone-other";
HPC.nf = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d).replace(".", ",");
HPC.pct = v => (v === null || v === undefined) ? "—" : `${Math.round(v * 100)}%`;
HPC.zoneEntries = zones => Object.entries(zones || {}).sort((a, b) => {
  const ia = HPC.ZONE_ORDER.indexOf(a[0]), ib = HPC.ZONE_ORDER.indexOf(b[0]);
  return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
});
// Pasek udziału stref w energii: segmenty szerokością ∝ kWh, opis tekstem pod paskiem (nie tylko kolor).
HPC.zoneBar = zones => {
  const rows = HPC.zoneEntries(zones).filter(([, z]) => z.kwh > 0);
  const total = rows.reduce((a, [, z]) => a + z.kwh, 0);
  if (!total) return "";
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const seg = rows.map(([n, z]) => `<i class="${HPC.zoneClass(n)}" style="flex:${z.kwh}" title="${esc(HPC.zoneLabel(n))}: ${HPC.nf(z.kwh)} kWh"></i>`).join("");
  const legend = rows.map(([n, z]) => `<span><i class="sw ${HPC.zoneClass(n)}"></i>${esc(HPC.zoneLabel(n))} ${HPC.nf(z.kwh)} kWh · ${HPC.nf(z.pln)} zł</span>`).join("");
  return `<div class="zonebar" role="img" aria-label="Energia wg stref taryfy">${seg}</div><p class="zonelegend">${legend}</p>`;
};

