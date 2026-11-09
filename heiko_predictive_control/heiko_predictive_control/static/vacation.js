// Kafel „Urlop” na pulpicie: status trybu urlopowego, parametry i bezpiecznik z 30 dni (GET /api/vacation).
(() => {
const body = document.getElementById("vacation-body");
const badge = document.getElementById("vacation-badge");
const summary = document.getElementById("vacation-summary");
if (!body || !badge || !summary) return;
const esc = HPC.esc, nf = HPC.nf;
const BADGE = { active: "ok", blocked: "warn", suspended: "warn", tripped: "warn" };

function fmtDate(iso) {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

function windowText(d) {
  if (d.from && d.to) return `${fmtDate(d.from)} – ${fmtDate(d.to)}`;
  if (d.from) return `od ${fmtDate(d.from)}`;
  if (d.to) return `do ${fmtDate(d.to)}`;
  return "bez ograniczenia";
}

function dhwText(d) {
  const warn = d.dhw_warn === "brak" ? " — brak odczytu nastawy CWU" : d.dhw_warn === "min" ? " — minimum encji CWU wyższe niż cel" : "";
  return `${esc(String(d.dhw))} °C${warn}`;
}

function render(d) {
  badge.textContent = d.status_label;
  badge.className = "badge" + (BADGE[d.status] ? " " + BADGE[d.status] : "");
  summary.textContent = d.summary || d.status_label;
  if (d.status === "off") {
    body.innerHTML = `<p class="sub">Tryb wyłączony — pompa grzeje według własnej krzywej.</p>`;
    return;
  }
  const r = d.trip_room;
  const trip = d.status === "tripped" ? `<p class="sub">Bezpiecznik: ${r && r.name ? `${esc(r.name)} (${nf(r.c, 1)} °C)` : "pokój"} był za zimny, przesunięcie wróciło do bazowego do ocieplenia.</p>` : "";
  body.innerHTML = `${trip}
    <table class="day-table"><tbody>
      <tr><th scope="row">Okno dat</th><td>${esc(windowText(d))}</td></tr>
      <tr><th scope="row">Obniżka krzywej</th><td>−${esc(String(d.drop))}</td></tr>
      <tr><th scope="row">CWU</th><td>${dhwText(d)}</td></tr>
      <tr><th scope="row">Minimum pokoju</th><td>${nf(d.min_room_c, 1)} °C</td></tr>
      <tr><th scope="row">Tryb Away</th><td>${d.with_away ? "tak" : "nie"}</td></tr>
      <tr><th scope="row">Docisk szczytu</th><td>${d.peak_pressing ? "tak" : "nie"}</td></tr>
    </tbody></table>
    <p class="sub">Zadziałania bezpiecznika w 30 dni: ${esc(String(d.trips_30d))}.</p>`;
}

async function load() {
  try { render(await HPC.getJSON("/api/vacation")); }
  catch (e) {
    console.error(e);
    badge.textContent = "błąd";
    summary.textContent = "Nie udało się wczytać stanu trybu urlopowego.";
    body.innerHTML = "";
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 300000);
})();
