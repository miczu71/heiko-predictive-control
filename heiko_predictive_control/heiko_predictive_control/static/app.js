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
