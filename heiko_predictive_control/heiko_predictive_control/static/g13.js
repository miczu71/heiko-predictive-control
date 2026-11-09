// Kafel „Szczyt G13” na pulpicie: stan trybu, następna zmiana, energia i koszt podłogówki w strefie szczytu (GET /api/g13).
(() => {
const body = document.getElementById("g13-body");
const badge = document.getElementById("g13-badge");
const summary = document.getElementById("g13-summary");
if (!body || !badge || !summary) return;
const esc = HPC.esc, nf = HPC.nf;
const BADGE = { off: "", blocked: "warn", suspended: "warn", tripped: "warn", active: "ok" };

function row(label, d) {
  return `<tr><th scope="row">${esc(label)}</th><td>${nf(d.kwh)} kWh</td><td>${nf(d.pln)} zł</td></tr>`;
}

function nextLine(d) {
  if (d.status === "off") return "Tryb wyłączony — pompa grzeje według własnej krzywej.";
  if (d.status === "blocked") return "Główny wyłącznik „Sterowanie aktywne (Heiko)” jest wyłączony — tryb nie zapisuje do pompy.";
  if (d.status === "suspended") return "Wstrzymany do jutra (ręczna zmiana przesunięcia albo odrzucony zapis).";
  if (d.status === "tripped") {
    const r = d.trip_room;
    const who = r && r.name ? `${esc(r.name)} (${nf(r.c, 1)} °C) był za zimny` : "pokój był za zimny";
    return `Bezpiecznik: ${who}, przesunięcie wróciło do bazowego do końca szczytu.`;
  }
  if (d.workday === null) return "Brak harmonogramu taryfy — tryb nic nie zmienia.";
  if (!d.workday) return "Brak szczytu evening_peak w taryfie dziś — bez zmian przesunięcia.";
  return d.next ? `Następna zmiana ${esc(d.next.at)} → ${esc(String(d.next.value).replace("-", "\u2212"))}` : "Dziś bez kolejnych zmian.";
}

function nowLine(d) {
  return d.now ? `<p class="sub">Teraz: przesunięcie krzywej ${esc(d.now.shift.replace("-", "\u2212"))}${d.now.base == null ? "" : ` (bazowe ${esc(d.now.base.replace("-", "\u2212"))})`} — ${esc(d.now.phase)}</p>` : "";
}

function render(d) {
  badge.textContent = d.status_label;
  badge.className = "badge" + (BADGE[d.status] ? " " + BADGE[d.status] : "");
  summary.innerHTML = nextLine(d);
  body.innerHTML = `${nowLine(d)}
    <table class="day-table"><thead><tr><th></th><th>Energia</th><th>Koszt</th></tr></thead>
    <tbody>${row("Dziś", d.today)}${row("7 dni", d.week)}</tbody></table>
    <p class="sub">Zadziałania bezpiecznika w 7 dni: ${d.trips_7d}. Energia i koszt wg stref taryfy, z zakończonych runów.</p>`;
}

async function load() {
  try { render(await HPC.getJSON("/api/g13")); }
  catch (e) {
    console.error(e);
    badge.textContent = "błąd";
    summary.textContent = "Nie udało się wczytać stanu trybu Szczyt G13.";
    body.innerHTML = "";
  }
}
load();
setInterval(() => { if (!document.hidden) load(); }, 300000);
})();
