const COLS_BY_LOOP = {
  heiko: [
    ["ts", "Czas"], ["active_profile", "Profil"], ["indoor_temp_c", "Wewn. °C"],
    ["outdoor_temp_c", "Zewn. °C"], ["setpoint_komfort", "Set. Komfort"],
    ["setpoint_ekonomia", "Set. Ekonomia"], ["price_pln_kwh", "Cena"],
  ],
  attic: [
    ["ts", "Czas"], ["phase", "Faza"], ["indoor_temp_c", "Wewn. °C"],
    ["ac_cmd_setpoint", "Nastawa AC"], ["offset_c", "Offset"],
    ["ac_power_w", "Moc W"], ["window_open", "Okno"], ["wrote", "Zapis"],
    ["attic_cost_pln", "Koszt PLN"],
  ],
};

async function loadHistory(loop) {
  const el = document.getElementById(`${loop}-history`);
  if (!el) return;
  try {
    const rows = await HPC.getJSON(`/api/history/${loop}?limit=100`);
    if (!rows.length) {
      el.textContent = "Brak jeszcze historii.";
      return;
    }
    const COLS = COLS_BY_LOOP[loop];
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
