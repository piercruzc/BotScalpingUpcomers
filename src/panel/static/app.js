const $ = (sel) => document.querySelector(sel);

const form = $("#paramsForm");
const modeButtons = [...document.querySelectorAll("#modeSwitch button")];
const dryRun = $("#dryRun");
const telegramRow = $("#telegramRow");
const signalCard = $("#signalCard");
const signalHint = $("#signalHint");
const planBox = $("#planBox");
const dialog = $("#realDialog");
const realConfirm = $("#realConfirm");

let state = { operating_mode: "demo" };
let formDirty = false;

function markFormDirty() {
  formDirty = true;
  $("#paramsDirty")?.classList.remove("hidden");
}

function clearFormDirty() {
  formDirty = false;
  $("#paramsDirty")?.classList.add("hidden");
}

function legsCount(settings) {
  return settings?.trail_enabled ? 4 : 3;
}

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.message || res.statusText);
  }
  return data;
}

function fillForm(settings) {
  if (formDirty || (form && form.contains(document.activeElement))) return;
  for (const [key, value] of Object.entries(settings)) {
    const field = form.elements[key];
    if (!field) continue;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value;
  }
  dryRun.checked = Boolean(settings.dry_run);
}

function updateLotKpi(settings) {
  const legs = legsCount(settings);
  $("#lotLine").textContent = `${settings.lot_size} × ${legs}`;
  const hint = $("#lotHint");
  if (hint) {
    hint.textContent = legs === 4
      ? "Cuatro entradas (trail TP4), mismo lote"
      : "Tres entradas, mismo lote";
  }
}

function setModeUi(mode, settings) {
  state.operating_mode = mode;
  document.body.dataset.mode = mode;
  modeButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
  const isDemo = mode === "demo";
  signalCard.classList.toggle("hidden", !isDemo);
  telegramRow.classList.toggle("hidden", !isDemo);
  $("#realBanner").classList.toggle("hidden", isDemo);
  if (!isDemo) {
    signalHint.textContent = "En Real las órdenes solo salen del canal de Telegram.";
  } else {
    signalHint.textContent = "Pega el texto del canal, previsualiza las entradas y ejecuta en la cuenta demo.";
  }
  if (formDirty) return;
  if (settings?.telegram_forced) {
    form.elements.telegram_enabled.checked = true;
    form.elements.telegram_enabled.disabled = true;
  } else {
    form.elements.telegram_enabled.disabled = false;
  }
}

function setLed(id, on) {
  const el = $(id);
  if (el) el.classList.toggle("on", Boolean(on));
}

function renderStatus(data) {
  const acc = data.account;
  const badge = acc.connected
    ? (acc.is_demo ? "DEMO" : "REAL")
    : "OFF";
  $("#accountLine").textContent = acc.connected
    ? `${badge} · ${acc.login} · ${acc.server} · bal ${acc.balance.toFixed(2)}`
    : acc.error || "Sin conexión";
  const align = $("#alignLine");
  align.textContent = data.alignment_error || (data.can_trade ? "Listo para operar" : (data.settings.dry_run ? "Simulación: no envía órdenes" : ""));
  align.classList.toggle("warn", Boolean(data.alignment_error));
  if (data.tick) {
    $("#tickLine").textContent = `bid ${data.tick.bid}  ask ${data.tick.ask}`;
    $("#spreadLine").textContent = `spread ${data.tick.spread_pips} pips`;
  } else {
    $("#tickLine").textContent = data.mt5_available ? "Sin tick" : "MetaTrader5 no está en este sistema (usa Windows)";
    $("#spreadLine").textContent = "";
  }
  const tg = data.telegram || {};
  $("#tgLine").textContent = tg.connected
    ? `Conectado · ${tg.channel || "canal"}`
    : "No conectado al canal";
  $("#tgDetail").textContent = tg.error || (tg.connected ? "Leyendo señales del VIP" : "python -m src.list_chats");
  setLed("#ledMt5", acc.connected);
  setLed("#ledTg", tg.connected);
  setLed("#ledReady", data.can_trade);
  updateLotKpi(data.settings);

  const rows = [
    ...(data.actives || []).map((a) => (
      `<div class="book-row"><span class="tag signal">Señal</span>${a.direction} @ ${a.entry} · TP1 ${a.tp1} TP2 ${a.tp2} · ${a.be_done ? "BE" : "SL orig"} ${a.tp2_done ? "· lock TP1" : ""}</div>`
    )),
    ...data.positions.map((p) => (
      `<div class="book-row"><span class="tag">Pos</span>${p.ticket} ${p.side} ${p.volume} @ ${p.price_open} SL ${p.sl} TP ${p.tp} ${p.comment || ""}</div>`
    )),
    ...data.pendings.map((p) => (
      `<div class="book-row"><span class="tag pend">Pend</span>${p.ticket} ${p.kind} ${p.volume} @ ${p.price} ${p.comment || ""}</div>`
    )),
  ];
  $("#books").innerHTML = rows.length
    ? rows.join("")
    : "<div class='muted'>Sin operaciones del bot</div>";
}

function renderPlan(plan) {
  if (plan.rejected) {
    planBox.innerHTML = `<div class="skip">${plan.rejected}</div>`;
    return;
  }
  planBox.innerHTML = plan.orders.map((o) => {
    const cls = o.skip_reason ? "skip" : "ok";
    const extra = o.skip_reason ? o.skip_reason : `${o.kind} @ ${o.entry}  TP ${o.tp ?? "trail"}  SL ${o.sl}  lot ${o.volume}`;
    return `<div class="${cls}">L${o.leg} ${o.side} — ${extra}</div>`;
  }).join("");
}

function renderLogs(items) {
  $("#logList").innerHTML = items.map((item) => {
    const t = item.time.slice(11, 19);
    return `<li class="${item.level}">${t} ${item.message}</li>`;
  }).join("");
}

function tickClock() {
  const el = $("#clock");
  if (!el) return;
  el.textContent = new Date().toLocaleTimeString("es-ES", { hour12: false });
}

async function refresh() {
  try {
    const [status, logs] = await Promise.all([api("/api/status"), api("/api/logs")]);
    fillForm(status.settings);
    setModeUi(status.settings.operating_mode, status.settings);
    renderStatus(status);
    renderLogs(logs.items);
  } catch (err) {
    $("#alignLine").textContent = err.message;
    $("#alignLine").classList.add("warn");
  }
}

modeButtons.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    if (mode === state.operating_mode) return;
    if (mode === "real") {
      realConfirm.value = "";
      dialog.showModal();
      return;
    }
    await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: "demo" }) });
    await refresh();
  });
});

$("#realForm").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "ok") return;
  event.preventDefault();
  try {
    await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode: "real", confirmation: realConfirm.value }),
    });
    dialog.close();
    await refresh();
  } catch (err) {
    alert(err.message);
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    lot_size: Number(form.lot_size.value),
    symbol: form.symbol.value,
    pip_size: Number(form.pip_size.value),
    max_spread_pips: Number(form.max_spread_pips.value),
    entry_tolerance: Number(form.entry_tolerance.value),
    near_entry_pips: Number(form.near_entry_pips.value),
    chase_buffer_pips: Number(form.chase_buffer_pips.value),
    be_cushion_pips: Number(form.be_cushion_pips.value),
    be_profit_pips: Number(form.be_profit_pips.value),
    deviation_points: Number(form.deviation_points.value),
    max_concurrent_signals: Number(form.max_concurrent_signals.value),
    trail_percent: Number(form.trail_percent.value),
    trail_start_pips: Number(form.trail_start_pips.value),
    trail_distance_pips: Number(form.trail_distance_pips.value),
    trail_enabled: form.trail_enabled.checked,
    telegram_enabled: form.telegram_enabled.checked,
    dry_run: dryRun.checked,
  };
  await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
  clearFormDirty();
  await refresh();
});

dryRun.addEventListener("change", async () => {
  await api("/api/config", { method: "PUT", body: JSON.stringify({ dry_run: dryRun.checked }) });
});

$("#previewBtn").addEventListener("click", async () => {
  try {
    const plan = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify({ text: $("#signalText").value }),
    });
    renderPlan(plan);
  } catch (err) {
    planBox.innerHTML = `<div class="skip">${err.message}</div>`;
  }
});

$("#executeBtn").addEventListener("click", async () => {
  try {
    const report = await api("/api/execute", {
      method: "POST",
      body: JSON.stringify({ text: $("#signalText").value }),
    });
    const prefix = report.dry_run ? "Simulación" : "Enviado";
    planBox.innerHTML = `<div class="ok">${prefix}: ${report.placed.length} órdenes</div>` +
      report.skipped.map((s) => `<div class="skip">L${s.leg} ${s.reason}</div>`).join("");
    await refresh();
  } catch (err) {
    planBox.innerHTML = `<div class="skip">${err.message}</div>`;
  }
});

tickClock();
setInterval(tickClock, 1000);
form.addEventListener("input", markFormDirty);
form.addEventListener("change", markFormDirty);
refresh();
setInterval(refresh, 2000);
