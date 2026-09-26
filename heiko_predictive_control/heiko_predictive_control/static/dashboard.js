// Pulpit 0.2.0: hotspoty modelu domu (akordeon kart szczegółów) + odświeżanie
// wartości na żywo z /api/live i kosztu z /api/today_summary co minutę.
const cards = Array.from(document.querySelectorAll(".detail[data-card]"));
const hotspots = Array.from(document.querySelectorAll(".house .hotspot[data-card]"));
const layout = document.querySelector(".house-layout");
const fmtPLN = (v) => `${v.toFixed(2).replace(".", ",")} zł`;
const fmtT = (v) => (v == null ? "—" : `${Number(v).toFixed(1).replace(".", ",")}°C`);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DAYS = ["Nd", "Pn", "Wt", "Śr", "Cz", "Pt", "So"];
const PROFILE_LABEL = { komfort: "Komfort", ekonomia: "Ekonomia" };

function openCard(key, source) {
  let shown = null;
  for (const card of cards) {
    const match = card.dataset.card === key;
    card.hidden = !match;
    if (match) shown = card;
  }
  for (const h of hotspots) {
    const active = source ? h === source : h.dataset.card === key && h.classList.contains("equip");
    h.classList.toggle("active", active);
    if (h.hasAttribute("role")) h.setAttribute("aria-expanded", String(active));
  }
  // Na telefonie karta jest pod wysokim modelem — przewiń do niej, jeśli jej nie widać.
  if (shown && source) {
    const r = shown.getBoundingClientRect();
    if (r.top > window.innerHeight || r.bottom < 0) {
      shown.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

for (const h of hotspots) {
  h.addEventListener("click", () => openCard(h.dataset.card, h));
  h.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openCard(h.dataset.card, h);
    }
  });
}

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

async function refreshLive() {
  let data;
  try {
    data = await HPC.getJSON("/api/live");
  } catch (e) {
    return; // chwilowy brak HA — zostają ostatnie wartości
  }
  for (const el of document.querySelectorAll("[data-live]")) {
    const v = getPath(data, el.dataset.live);
    if (v !== undefined && v !== null) el.textContent = v;
  }
  for (const el of document.querySelectorAll("[data-tclass]")) {
    const room = data.rooms[el.dataset.tclass];
    if (!room) continue;
    el.classList.remove("t-cold", "t-cool", "t-ok", "t-warm", "t-none");
    el.classList.add(room.cls);
  }
  for (const el of document.querySelectorAll("[data-on]")) {
    const eq = data.equipment[el.dataset.on];
    if (eq) el.classList.toggle("is-on", eq.on);
  }
  const tariff = document.getElementById("tariff-label");
  if (tariff) tariff.classList.toggle("peak", data.tariff.peak === true);
}

async function refreshCost() {
  const heikoEl = document.getElementById("heiko-cost");
  try {
    const s = await HPC.getJSON("/api/today_summary/heiko");
    const profile = layout ? layout.dataset.heikoProfile : "ekonomia";
    const savings = profile === "komfort" ? s.savings_komfort_pln : s.savings_ekonomia_pln;
    document.getElementById("cost-baseline").textContent = s.cycles ? fmtPLN(s.actual_pln) : "—";
    document.getElementById("cost-savings").textContent = s.cycles ? fmtPLN(savings) : "—";
    if (heikoEl) {
      heikoEl.innerHTML = s.cycles
        ? `Dziś realnie (licznik): <strong>${fmtPLN(s.actual_pln)}</strong><br>
           Model — krzywa natywna: ${fmtPLN(s.baseline_pln)}<br>
           Model — Komfort: ${fmtPLN(s.komfort_pln)} (różnica ${fmtPLN(s.savings_komfort_pln)})<br>
           Model — Ekonomia: ${fmtPLN(s.ekonomia_pln)} (różnica ${fmtPLN(s.savings_ekonomia_pln)})`
        : "Za mało danych na dziś — koszt pojawi się po drugim cyklu (15 min).";
    }
  } catch (e) {
    if (heikoEl) heikoEl.textContent = "Błąd wczytywania kosztu.";
  }
}

// ── Plan pętli A: wykres przewidywanej temperatury, oszczędność, bloki, model ──

function svgChart(plan, active) {
  const hours = plan.hours || [];
  if (hours.length < 2) return "";
  const W = 600, H = 200, L = 36, R = 8, T = 10, B = 24;
  const keys = ["base_temp", "komfort_temp", "ekonomia_temp", "band_lo", "band_hi"];
  const vals = hours.flatMap((h) => keys.map((k) => h[k])).filter((v) => v != null);
  const lo = Math.floor((Math.min(...vals) - 0.2) * 2) / 2;
  const hi = Math.ceil((Math.max(...vals) + 0.2) * 2) / 2;
  const n = hours.length;
  const x = (i) => L + (i * (W - L - R)) / (n - 1);
  const y = (v) => T + ((hi - v) * (H - T - B)) / (hi - lo);
  const t0 = new Date(plan.t0).getTime();
  const idxOf = (iso) => Math.min(n - 1, Math.max(0, (new Date(iso).getTime() - t0) / 3600000 - 1));
  const peaks = (plan.blocks || []).filter((b) => b.peak).map((b) => {
    const a = x(idxOf(b.start)), z = x(idxOf(b.end));
    return `<rect class="pc-peak" x="${a.toFixed(1)}" y="${T}" width="${Math.max(z - a, 0).toFixed(1)}" height="${H - T - B}"/>`;
  }).join("");
  const line = (key) => "M" + hours.map((h, i) => `${x(i).toFixed(1)},${y(h[key]).toFixed(1)}`).join("L");
  const band = "M" + hours.map((h, i) => `${x(i).toFixed(1)},${y(h.band_hi).toFixed(1)}`).join("L")
    + "L" + hours.slice().reverse().map((h, j) => `${x(n - 1 - j).toFixed(1)},${y(h.band_lo).toFixed(1)}`).join("L") + "Z";
  const yticks = [];
  for (let v = Math.ceil(lo); v <= hi; v += 1) yticks.push(v);
  const yt = yticks.map((v) => `<line class="pc-grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>
    <text class="pc-tick" x="${L - 4}" y="${y(v) + 3}" text-anchor="end">${v}°</text>`).join("");
  const xt = hours.map((h, i) => ({ h, i })).filter(({ h }) => h.t.slice(11, 13) % 6 === 0 && h.t.slice(14, 16) === "00")
    .map(({ h, i }) => `<text class="pc-tick" x="${x(i)}" y="${H - 6}" text-anchor="middle">${h.t.slice(11, 16)}</text>`).join("");
  const other = active === "komfort" ? "ekonomia" : "komfort";
  return `<svg viewBox="0 0 ${W} ${H}" role="img" preserveAspectRatio="xMidYMid meet">
    ${peaks}<path class="pc-band" d="${band}"/>${yt}${xt}
    <path class="pc-line pc-base" d="${line("base_temp")}"/>
    <path class="pc-line pc-other" d="${line(other + "_temp")}"/>
    <path class="pc-line pc-active" d="${line(active + "_temp")}"/>
  </svg>
  <figcaption><span><i class="pc-key pc-base"></i>krzywa natywna</span>
    <span><i class="pc-key pc-active"></i>plan ${PROFILE_LABEL[active]}</span>
    <span><i class="pc-key pc-other"></i>plan ${PROFILE_LABEL[other]}</span>
    <span><i class="pc-key pc-bandkey"></i>pasmo komfortu</span>
    <span><i class="pc-key pc-peakkey"></i>szczyt taryfy</span></figcaption>`;
}

function renderKpi(data) {
  const k = data.kpi, b = data.kpi_baseline;
  const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1).replace(".", ",")}%`);
  if (!b) {
    return `<p class="note">Baza z zimy jest jeszcze liczona (pierwsze uruchomienie, kilka minut).</p>`;
  }
  const base = `Zima sprzed sterowania: ${pct(b.overall)} energii w szczycie (${b.hours} h danych, ${esc(b.at.slice(0, 10))}).`;
  if (!k) {
    return `<p>Za mało energii w ostatnich 7 dniach (poza sezonem grzewczym) — KPI pojawi się, gdy pompa zacznie grzać.</p>
      <p class="note">${base}</p>`;
  }
  const delta = k.delta_pp == null ? "—" : `${k.delta_pp > 0 ? "+" : ""}${k.delta_pp.toFixed(1).replace(".", ",")} p.p.`;
  return `<dl class="kv">
    <dt>Ostatnie 7 dni</dt><dd>${pct(k.share)} <small>(${k.kwh.toFixed(1).replace(".", ",")} kWh)</small></dd>
    <dt>Zima przy tej samej pogodzie</dt><dd>${pct(k.baseline_share)}</dd>
    <dt>Przesunięcie poza szczyt</dt><dd>${delta}</dd></dl>
    <p class="note">${base} Energia łącznie z CWU po obu stronach porównania; święta liczone jak dni robocze.</p>`;
}

function modelCaveat(model) {
  if (model.identified) return "";
  if (model.source === "domyślny") {
    return "Model niedopasowany — parametry domyślne. Liczby poniżej są czysto orientacyjne.";
  }
  if (model.source.startsWith("bilans")) {
    return "Bilans energii domu jest dopasowany do danych, ale bezwładność (c, τ) wynika z ograniczeń, "
      + "nie z pomiaru — zimowe dane w pętli zamkniętej jej nie rozstrzygają. Oszczędności są "
      + "orientacyjne do czasu sterowania aktywnego.";
  }
  return "";
}

function renderSavings(plan, model) {
  const b = plan.baseline;
  const caveat = model ? modelCaveat(model) : "";
  const rows = ["komfort", "ekonomia"].map((name) => {
    const p = plan.summary[name];
    return `<tr><th>${PROFILE_LABEL[name]}</th><td>${fmtPLN(p.saving_pln)}</td>
      <td>${fmtPLN(p.saving_shift_pln)}</td><td>${fmtT(p.mean_temp_c)}</td><td>${fmtT(p.min_temp_c)}</td></tr>`;
  }).join("");
  return `${caveat ? `<p class="warn">${esc(caveat)}</p>` : ""}<table><thead><tr><th></th><th>Oszczędność ${plan.horizon_h} h</th>
    <th>w tym z samego przesunięcia</th><th>Śr. temp.</th><th>Min. temp.</th></tr></thead>
    <tbody><tr><th>Krzywa natywna</th><td>—</td><td>—</td><td>${fmtT(b.mean_temp_c)}</td><td>${fmtT(b.min_temp_c)}</td></tr>${rows}</tbody></table>
    <p class="note">„Z samego przesunięcia” = ten sam plan przy średniej temperaturze nie niższej niż na krzywej natywnej.
      Reszta oszczędności wynika z chłodniejszego domu (w granicach pasma).</p>`;
}

function renderBlocks(plan, active) {
  const rows = (plan.blocks || []).map((b) => {
    const d = new Date(b.start);
    const cell = (name) => {
      const delta = b[name + "_delta"];
      const sign = delta > 0 ? `+${delta}` : `${delta}`;
      return `<td class="${name === active ? "on" : ""}">${fmtT(b[name + "_c"])} <small>(${sign})</small></td>`;
    };
    return `<tr class="${b.peak ? "peak" : ""}"><td>${DAYS[d.getDay()]} ${b.start.slice(11, 16)}–${b.end.slice(11, 16)}</td>
      <td>${b.peak ? "szczyt" : "tania"}</td><td>${fmtT(b.base_c)}</td>${cell("komfort")}${cell("ekonomia")}</tr>`;
  }).join("");
  return `<table><thead><tr><th>Blok</th><th>Taryfa</th><th>Krzywa</th><th>Komfort</th><th>Ekonomia</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderModel(data) {
  const m = data.model, boot = data.bootstrap, refit = data.refit;
  const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d).replace(".", ","));
  const bootTxt = boot ? `${boot.ok ? "udany" : "nieudany"} — ${esc(boot.reason)}` : "jeszcze nie próbowany";
  const refitTxt = refit ? `${esc(refit.at.slice(0, 16).replace("T", " "))}: ${esc(refit.reason)}` : "jeszcze nie było";
  return `<dl class="kv">
    <dt>Źródło parametrów</dt><dd>${esc(m.source)}${m.fitted_at ? ` (${esc(m.fitted_at.slice(0, 10))})` : ""}</dd>
    <dt>Stała czasowa wylewki τ</dt><dd>${num(m.tau_h, 1)} h</dd>
    <dt>Strata cieplna c</dt><dd>${num(m.c, 3)} /h</dd>
    <dt>Zysk g</dt><dd>${num(m.g, 3)} °C/h na kW</dd>
    <dt>Moc grzania e</dt><dd>${num(m.e, 2)} kW/K</dd>
    <dt>Błąd prognozy 6 h (dopasowanie)</dt><dd>${m.rmse_c == null ? "—" : num(m.rmse_c) + " °C"} <small>(bez modelu: ${num(m.persist_c)})</small></dd>
    <dt>Błąd 1 kroku na żywo (24 h)</dt><dd>${data.live_rmse_c == null ? "—" : num(data.live_rmse_c, 3) + " °C"} <small>(${data.live_samples} prób)</small></dd>
    <dt>Bezwładność domu (c, τ)</dt><dd>${m.identified ? "zidentyfikowana z wymuszenia" : "niezidentyfikowana"}</dd>
    ${refit && refit.excitation ? `<dt>Wymuszenie (14 dni)</dt><dd>${refit.excitation.transitions} przejść, śr. spadek ${num(refit.excitation.mean_drop_c, 1)} °C${refit.excitation.sufficient ? "" : " <small>(za mało)</small>"}</dd>` : ""}
    <dt>Bootstrap z zimy</dt><dd>${bootTxt}</dd>
    <dt>Ostatnie dobowe dopasowanie</dt><dd>${refitTxt}</dd>
  </dl>`;
}

async function refreshPlan() {
  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  let data;
  try {
    data = await HPC.getJSON("/api/plan");
  } catch (e) {
    set("heiko-savings", "Błąd wczytywania planu.");
    return;
  }
  set("heiko-model", renderModel(data));
  set("heiko-kpi", renderKpi(data));
  const plan = data.plan;
  if (!plan || !plan.hours) {
    const empty = "Plan pojawi się po pierwszym cyklu (do 15 min) — wymaga punktów krzywej grzewczej.";
    set("heiko-chart", ""); set("heiko-savings", empty); set("heiko-blocks", empty);
    return;
  }
  const active = layout ? layout.dataset.heikoProfile : "ekonomia";
  set("heiko-chart", svgChart(plan, active));
  set("heiko-savings", renderSavings(plan, data.model));
  set("heiko-blocks", renderBlocks(plan, active));
  const fuse = document.getElementById("hp-fuse");
  if (fuse) {
    fuse.hidden = !plan.fuse_active;
    fuse.textContent = plan.fuse_active
      ? `Bezpiecznik: ${plan.min_room_name} ma ${fmtT(plan.min_room_c)} — poniżej minimum. Plan nie obniża bieżącego bloku.` : "";
  }
}

openCard("loop_heiko", null);
refreshCost();
refreshPlan();
setInterval(() => { refreshLive(); refreshCost(); refreshPlan(); }, 60_000);
