// Pulpit 0.2.0: hotspoty modelu domu (akordeon kart szczegółów) + odświeżanie
// wartości na żywo z /api/live i kosztu z /api/today_summary co minutę.
const cards = Array.from(document.querySelectorAll(".detail[data-card]"));
const hotspots = Array.from(document.querySelectorAll(".house .hotspot[data-card]"));
const layout = document.querySelector(".house-layout");
const fmtPLN = (v) => `${v.toFixed(2).replace(".", ",")} zł`;

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
    document.getElementById("cost-baseline").textContent = s.cycles ? fmtPLN(s.baseline_pln) : "—";
    document.getElementById("cost-savings").textContent = s.cycles ? fmtPLN(savings) : "—";
    if (heikoEl) {
      heikoEl.innerHTML = s.cycles
        ? `Dziś realnie: <strong>${fmtPLN(s.baseline_pln)}</strong><br>
           Komfort: ${fmtPLN(s.komfort_pln)} (oszczędność ${fmtPLN(s.savings_komfort_pln)})<br>
           Ekonomia: ${fmtPLN(s.ekonomia_pln)} (oszczędność ${fmtPLN(s.savings_ekonomia_pln)})`
        : "Za mało danych na dziś — koszt pojawi się po drugim cyklu (15 min).";
    }
  } catch (e) {
    if (heikoEl) heikoEl.textContent = "Błąd wczytywania kosztu.";
  }
}

openCard("loop_heiko", null);
refreshCost();
setInterval(() => { refreshLive(); refreshCost(); }, 60_000);
