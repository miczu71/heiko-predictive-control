const form = document.getElementById("options-form");
const status = document.getElementById("save-status");

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) {
    payload[key] = value;
  }
  // Checkboxy nieodhaczone nie trafiają do FormData — dopisujemy jawnie false.
  for (const cb of form.querySelectorAll('input[type=checkbox]')) {
    if (!(cb.name in payload)) payload[cb.name] = false;
    else payload[cb.name] = true;
  }
  // Pola liczbowe jako number, nie string.
  for (const input of form.querySelectorAll('input[type=number]')) {
    if (input.name in payload) payload[input.name] = parseFloat(payload[input.name]);
  }
  status.textContent = "Zapisywanie…";
  try {
    await HPC.postJSON("/api/settings", payload);
    status.textContent = "Zapisano.";
  } catch (e) {
    status.textContent = "Błąd zapisu.";
  }
  setTimeout(() => { status.textContent = ""; }, 3000);
});
