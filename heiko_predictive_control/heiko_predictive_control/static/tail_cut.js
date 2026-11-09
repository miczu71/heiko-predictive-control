// Kafel „Ogon grzania” na pulpicie: stan odcinania końcówki cyklu, odliczanie i liczniki odcięć (GET /api/tail_cut).
(() => {
const body = document.getElementById("tail-cut-body");
const badge = document.getElementById("tail-cut-badge");
const summary = document.getElementById("tail-cut-summary");
if (!body || !badge || !summary) return;
const esc = HPC.esc;
const BADGE = { off: "", blocked: "warn", holding: "warn", pause: "", watching: "ok" };

function hm(iso) { return iso.slice(11, 16); }
function dm(iso) { return `${iso.slice(8, 10)}.${iso.slice(5, 7)} ${iso.slice(11, 16)}`; }

function stateLine(d) {
  if (d.status === "off") return "Wyłączone — pompa sama kończy cykl.";
  if (d.status === "blocked") return "Główny wyłącznik „Sterowanie aktywne (Heiko)” jest wyłączony — ogon nie jest odcinany.";
  if (d.status === "holding") return `Trzymam CWU${d.last_cut ? " od " + esc(hm(d.last_cut)) : ""}, powrót do Auto po ${esc(d.hold_min)} min.`;
  if (d.status === "pause") return d.pause_until ? `Przerwa do ${esc(hm(d.pause_until))}.` : "Przerwa po odcięciu.";
  return d.tail_minutes != null
    ? `Odliczanie ${esc(d.tail_minutes)} min (próg ${esc(d.delay_min)} min).`
    : "Czekam na ogon.";
}

function render(d) {
  badge.textContent = d.status_label;
  badge.className = "badge" + (BADGE[d.status] ? " " + BADGE[d.status] : "");
  const last = d.last_cut ? dm(d.last_cut) : "brak";
  summary.innerHTML = stateLine(d);
  body.innerHTML = `<table class="day-table"><thead><tr><th></th><th>Odcięcia</th></tr></thead>
    <tbody><tr><th scope="row">Dziś</th><td>${esc(d.cuts_today)}</td></tr>
    <tr><th scope="row">7 dni</th><td>${esc(d.cuts_7d)}</td></tr>
    <tr><th scope="row">Łącznie</th><td>${esc(d.cuts_total)}</td></tr></tbody></table>
    <p class="sub">Ostatnie odcięcie: ${esc(last)}</p>`;
}

async function load() {
  try { render(await HPC.getJSON("/api/tail_cut")); }
  catch (e) {
    console.error(e);
    badge.textContent = "błąd";
    summary.textContent = "Nie udało się wczytać stanu ogona grzania.";
    body.innerHTML = "";
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 60000);
})();
