"use strict";

const element = (id) => document.getElementById(id);
const statusNames = {
  idle: "Готов к запуску", starting: "Подключение…", running: "Сценарий идёт",
  finished: "Сценарий завершён", stopped: "Остановлен", error: "Ошибка",
};
const modes = {
  ADAPTIVE: "Адаптивный", COORDINATED: "Зелёная волна", CONGESTION: "Разгрузка затора",
  FIXED: "Резервный", EMERGENCY: "Зелёный коридор",
};
const events = {
  scenario_started: "Запуск сценария", scenario_finished: "Сценарий завершён",
  traffic_data: "Показания датчика", invalid_measurement: "Некорректный замер",
  valid_measurement: "Корректный замер", sensor_failure: "Отказ датчика",
  sensor_restored: "Датчик восстановлен", mode_changed: "Смена режима",
  intersection_status: "Отчёт о перекрёстке", apply_plan: "Предложен план",
  plan_reply: "Ответ на план", commit_plan: "Согласованный план",
  plan_active: "План вступил в силу", cancel_plan: "План отменён",
  wave_detected: "Обнаружен преобладающий поток", congestion_detected: "Обнаружен затор",
  signal: "Переключение сигнала", green_extended: "Зелёный продлён",
  emergency_started: "Выезд скорой", emergency_finished: "Маршрут завершён",
  corridor_request: "Запрос зелёного коридора", corridor_update: "Уточнение времени прибытия",
  corridor_reply: "Ответ на запрос коридора", corridor_ready: "Зелёный коридор готов",
  corridor_cancel: "Коридор снят", corridor_unavailable: "Приоритет завершён",
  vehicle_passed: "Скорая проехала перекрёсток", dispatch_warning: "Предупреждение диспетчеру",
  run_error: "Ошибка запуска",
};
const stageNames = {green: "зелёный", yellow: "жёлтый", all_red: "все красные"};
const axisNames = {NS: "север–юг", EW: "восток–запад"};
const positions = {P1: [224, 151], P2: [536, 151], P3: [224, 362], P4: [536, 362]};
const sideNames = {N: "С", S: "Ю", W: "З", E: "В"};
const signalColors = {green: "#75efab", yellow: "#ffd270", red: "#ff696f"};
const approaches = {
  N: {dx: 0, dy: -1, laneX: -13, laneY: 0, angle: Math.PI / 2, signalX: -58, signalY: -52},
  S: {dx: 0, dy: 1, laneX: 13, laneY: 0, angle: -Math.PI / 2, signalX: 58, signalY: 52},
  W: {dx: -1, dy: 0, laneX: 0, laneY: 13, angle: 0, signalX: -58, signalY: 52},
  E: {dx: 1, dy: 0, laneX: 0, laneY: -13, angle: Math.PI, signalX: 58, signalY: -52},
};
const stopOffset = 34;
const stopDistance = 17;
const blockDistance = 500;
const carColors = ["#afcbd5", "#f2b778", "#e3dfb7", "#91c2ab", "#c3a8ef"];
const motion = {targets: new Map(), started: 0, duration: 0};
const initial = Object.fromEntries(Object.keys(positions).map((id) => [
  id, {axis: "NS", stage: "green", mode: "ADAPTIVE", sensor: "OK",
       approaches: Object.fromEntries(Object.keys(sideNames).map((side) => [side, {queue: 0}]))},
]));
const canvas = element("map");
const ctx = canvas.getContext("2d");
let scenarios = [];
let current = {status: "idle", crossings: initial};
let timeline = [];
let runId = "";
let cursor = 0;
let busy = false;
let requestError = "";

const clock = (second) => [Math.floor(second / 60), second % 60]
  .map((part) => String(part).padStart(2, "0")).join(":");
const crossName = (id) => id.replace("P", "П");

function rounded(x, y, w, h, radius, color) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, radius);
  ctx.fill();
}
function line(x1, y1, x2, y2, color, width, dash = []) {
  ctx.save();
  ctx.setLineDash(dash);
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.restore();
}
function text(value, x, y, color = "#a5b7b7", size = 15) {
  ctx.fillStyle = color;
  ctx.font = `600 ${size}px system-ui`;
  ctx.textAlign = "center";
  ctx.fillText(value, x, y);
}
function signalFor(crossing, axis) {
  return crossing.axis === axis && crossing.stage !== "all_red" ? crossing.stage : "red";
}

function approachSignal(x, y, color, side, count) {
  const width = count ? 53 : 36;
  rounded(x - width / 2, y - 10, width, 20, 5, "#102023");
  const left = x - width / 2;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(left + 10, y, 4.6, 0, Math.PI * 2);
  ctx.fill();
  text(sideNames[side], left + 24, y + 4, "#e4eeeb", 11);
  if (count) text(String(count), left + 42, y + 4, "#e4eeeb", 11);
}

function car(x, y, angle, color, ambulance = false) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(angle);
  rounded(-9, -5, 18, 10, 2.8, color);
  rounded(2, -3, 3, 6, 1, "#2b444a");
  if (ambulance) {
    rounded(-5, -1, 6, 2, 0.5, "#ec5160");
    rounded(-3, -3, 2, 6, 0.5, "#ec5160");
    rounded(5, -6, 3, 2, 1, "#77bcfb");
    rounded(5, 4, 3, 2, 1, "#77bcfb");
  }
  ctx.restore();
}

function roadProgress(distance, gap) {
  // Expand the intersection on screen while keeping both stop lines aligned.
  const edge = (gap - 2 * stopOffset) / (blockDistance - 2 * stopDistance);
  if (distance < -stopDistance) return -stopOffset + (distance + stopDistance) * edge;
  if (distance <= stopDistance) return distance * stopOffset / stopDistance;
  if (distance < blockDistance - stopDistance) return stopOffset + (distance - stopDistance) * edge;
  if (distance <= blockDistance + stopDistance) return gap + (distance - blockDistance) * stopOffset / stopDistance;
  return gap + stopOffset + (distance - blockDistance - stopDistance) * edge;
}

function blend(now) {
  return motion.duration ? Math.min(1, (now - motion.started) / motion.duration) : 1;
}

function interpolated(vehicle, fraction) {
  return vehicle.from + (vehicle.position - vehicle.from) * fraction;
}

function updateMotion(snapshot, reset) {
  if (!reset && snapshot.time === current.time && snapshot.status === current.status) return;
  const now = performance.now();
  const fraction = blend(now);
  const vehicles = [...(snapshot.vehicles || [])];
  if (snapshot.ambulance) {
    vehicles.push({
      id: "ambulance", side: "W", first: "P1", position: snapshot.ambulance.x * blockDistance,
    });
  }
  const frames = new Map();
  for (const vehicle of vehicles) {
    const previous = reset ? null : motion.targets.get(vehicle.id);
    frames.set(vehicle.id, {
      ...vehicle,
      from: previous ? interpolated(previous, fraction) : vehicle.position,
    });
  }
  motion.targets = frames;
  motion.started = now;
  const elapsed = (snapshot.time - current.time) * 1000 / (snapshot.speed || 4);
  motion.duration = reset || snapshot.status !== "running" ? 0 : Math.max(80, Math.min(1000, elapsed || 0));
}

function drawVehicles(now) {
  const fraction = blend(now);
  for (const vehicle of motion.targets.values()) {
    const [x, y] = positions[vehicle.first];
    const {dx, dy, laneX, laneY, angle} = approaches[vehicle.side];
    const gap = dx ? positions.P2[0] - positions.P1[0] : positions.P3[1] - positions.P1[1];
    const progress = roadProgress(interpolated(vehicle, fraction), gap);
    const ambulance = vehicle.id === "ambulance";
    const px = x - dx * progress + laneX;
    const py = y - dy * progress + (ambulance ? 5 : laneY);
    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(angle);
    ctx.scale(ambulance ? 1.33 : dx ? .58 : .38, ambulance ? 1.33 : .65);
    car(0, 0, 0, ambulance ? "#fff3ee" : carColors[vehicle.id % carColors.length], ambulance);
    ctx.restore();
    if (ambulance) text("Скорая", px, py + 37, "#f8e4dc", 15);
  }
}

function drawMap(now) {
  const snapshot = current;
  ctx.fillStyle = "#142323";
  ctx.fillRect(0, 0, 760, 494);
  for (let x = 12; x < 760; x += 22) {
    for (let y = 12; y < 494; y += 22) {
      ctx.fillStyle = "#293f3b";
      ctx.fillRect(x, y, 1.4, 1.4);
    }
  }
  for (const y of [151, 362]) {
    line(0, y, 760, y, "#465452", 64);
    line(0, y, 760, y, "#2c3a3c", 60);
    line(0, y, 760, y, "#67716b", 1.2, [11, 10]);
  }
  for (const x of [224, 536]) {
    line(x, 0, x, 494, "#465452", 64);
    line(x, 0, x, 494, "#2c3a3c", 60);
    line(x, 0, x, 494, "#67716b", 1.2, [11, 10]);
  }
  text("проспект 1", 224, 27);
  text("проспект 2", 536, 27);
  text("ул. Первая", 382, 104);
  text("ул. Вторая", 382, 315);
  for (const [id, [x, y]] of Object.entries(positions)) {
    const crossing = (snapshot.crossings || initial)[id];
    rounded(x - 31, y - 31, 62, 62, 1, "#2c3a3c");
    for (const [side, {dx, dy, laneX, laneY, signalX, signalY}] of Object.entries(approaches)) {
      const axis = dy ? "NS" : "EW";
      const stopX = x + dx * stopOffset + laneX;
      const stopY = y + dy * stopOffset + laneY;
      line(stopX - dy * 11, stopY - dx * 11, stopX + dy * 11, stopY + dx * 11, "#bac2ba", 1.5);
      approachSignal(x + signalX, y + signalY, signalColors[signalFor(crossing, axis)],
                     side, crossing.approaches[side].queue);
    }
    rounded(x - 17, y - 10, 34, 20, 5, "#152528");
    text(crossName(id), x, y + 5, "#f3f6f6", 13);
  }
  if (snapshot.scenario === "UC-05") {
    line(25, 151, 725, 151, "#7cbddb", 1, [6, 7]);
  }
  drawVehicles(now);
}

function animate(now) {
  if (motion.duration) {
    drawMap(now);
    if (now >= motion.started + motion.duration) motion.duration = 0;
  }
  requestAnimationFrame(animate);
}

function renderCrossings() {
  const cards = Object.entries(current.crossings || initial).map(([id, crossing]) => {
    const card = document.createElement("article");
    card.className = "crossing";
    const title = document.createElement("strong");
    title.textContent = crossName(id);
    const signals = Object.entries({NS: "С–Ю", EW: "В–З"}).map(([axis, label]) => {
      const state = document.createElement("small");
      state.className = "axis-signal";
      state.dataset.signal = signalFor(crossing, axis);
      const names = {...stageNames, red: "красный"};
      state.textContent = `${label} · ${names[state.dataset.signal]}`;
      return state;
    });
    const mode = document.createElement("small");
    mode.textContent = modes[crossing.mode] + (crossing.sensor === "FAILED" ? " · датчик отказал" : "");
    if (crossing.sensor === "FAILED") mode.className = "failed";
    const queue = document.createElement("small");
    queue.textContent = Object.entries(sideNames)
      .map(([side, name]) => `${name} ${crossing.approaches[side].queue}`).join(" · ");
    queue.setAttribute("aria-label", "Очереди: " + queue.textContent);
    card.append(title, ...signals, mode, queue);
    return card;
  });
  element("crossings").replaceChildren(...cards);
}

function eventInfo(row) {
  const data = row.data;
  const info = [row.sender + (row.receiver ? " → " + row.receiver : "")];
  if (data.mode) info.push(modes[data.mode]);
  if (data.stage) info.push(stageNames[data.stage]);
  if (data.axis && data.stage !== "all_red") info.push(axisNames[data.axis]);
  if (data.decision) info.push(data.decision);
  if (data.crossing) info.push(crossName(data.crossing));
  if (data.green !== undefined) info.push(`зелёный ${data.green} с`);
  if (data.eta !== undefined) info.push(`прибытие через ${data.eta} с`);
  if (data.offset !== undefined) info.push(`сдвиг ${data.offset} с`);
  if (data.fill !== undefined) info.push(`заполнение ${Math.round(data.fill * 100)}%`);
  if (data.lengths) info.push(`С–Ю ${data.lengths.NS} с, В–З ${data.lengths.EW} с`);
  return info.join(" · ");
}
function renderFeed() {
  const detailed = element("detail").checked;
  const visible = timeline.filter((row) => detailed || row.event !== "traffic_data").slice(-70).reverse();
  element("event-total").textContent = `Записей: ${cursor}`;
  element("empty").hidden = visible.length > 0;
  element("feed").replaceChildren(...visible.map((row) => {
    const line = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = clock(row.time);
    const content = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = events[row.event] || row.event;
    const detail = document.createElement("small");
    detail.textContent = eventInfo(row);
    content.append(title, detail);
    line.append(time, content);
    return line;
  }));
}

function controls() {
  const active = ["running", "starting"].includes(current.status);
  const status = element("status");
  status.textContent = statusNames[current.status];
  status.dataset.state = current.status;
  element("start").disabled = active || busy || !scenarios.length;
  element("stop").disabled = !active || busy;
  element("scenario").disabled = active || busy;
  element("speed").disabled = active || busy;
  element("download").hidden = !runId || cursor === 0;
  element("description").textContent =
    scenarios.find((scenario) => scenario.id === element("scenario").value)?.description || "";
  element("error").textContent = requestError || (current.error &&
    "Ошибка запуска сценария. Проверьте RabbitMQ. " + current.error) || "";
  element("error").hidden = !element("error").textContent;
}

function render() {
  controls();
  const time = current.time || 0;
  element("time").textContent = clock(time);
  element("progress").max = current.duration || 1;
  element("progress").value = time;
  drawMap(performance.now());
  renderCrossings();
  renderFeed();
}

async function api(path, options) {
  const response = await fetch(path, {cache: "no-store", ...options});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `Ошибка HTTP ${response.status}`);
  }
  return response.json();
}

async function refresh() {
  const state = await api(`/api/state?after=${cursor}&run_id=${encodeURIComponent(runId)}`);
  updateMotion(state, state.run_id !== runId);
  if (state.run_id !== runId) {
    timeline = [];
    runId = state.run_id || "";
  }
  cursor = state.event_count;
  timeline.push(...state.events);
  timeline = timeline.slice(-600);
  if (current.status === "idle" && state.scenario) element("scenario").value = state.scenario;
  current = state;
}

async function changeRun(path, data) {
  busy = true;
  requestError = "";
  controls();
  try {
    await api(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    await refresh();
  } catch (error) {
    requestError = error.message;
  } finally {
    busy = false;
    render();
  }
}
element("start").addEventListener("click", () => changeRun("/api/run", {
  scenario: element("scenario").value, speed: Number(element("speed").value),
}));
element("stop").addEventListener("click", () => changeRun("/api/stop"));
element("scenario").addEventListener("change", controls);
element("detail").addEventListener("change", renderFeed);

async function poll() {
  if (!busy) {
    try {
      if (!scenarios.length) {
        scenarios = await api("/api/scenarios");
        for (const scenario of scenarios) {
          element("scenario").add(new Option(`${scenario.id} · ${scenario.title}`, scenario.id));
        }
      }
      await refresh();
    } catch {
      if (!requestError) requestError = "Проверьте, запущено ли приложение.";
    }
    render();
  }
  setTimeout(poll, 100);
}
render();
requestAnimationFrame(animate);
poll();
