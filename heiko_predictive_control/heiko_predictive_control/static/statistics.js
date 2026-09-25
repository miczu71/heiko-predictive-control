const COLS = [
  ["ts", "Czas"], ["active_profile", "Profil"], ["indoor_temp_c", "Wewn. °C"],
  ["outdoor_temp_c", "Zewn. °C"], ["setpoint_komfort", "Set. Komfort"],
  ["setpoint_ekonomia", "Set. Ekonomia"], ["price_pln_kwh", "Cena"],
];

async function loadHistory(loop) {
  const el = document.getElementById(`${loop}-history`);
  if (!el) return;
  try {
    const rows = await HPC.getJSON(`/api/history/${loop}?limit=100`);
    if (!rows.length) {
      el.textContent = "Brak jeszcze historii.";
      return;
    }
    const thead = `<tr>${COLS.map(([, label]) => `<th>${label}</th>`).join("")}</tr>`;
    const tbody = rows.map(r => `<tr>${COLS.map(([key]) => {
      const v = r[key];
      return `<td>${v === null || v === undefined ? "—" : v}</td>`;
    }).join("")}</tr>`).join("");
    el.innerHTML = `<table><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
  } catch (e) {
    el.textContent = "Błąd wczytywania historii.";
  }
}

loadHistory("heiko");
loadHistory("attic");
