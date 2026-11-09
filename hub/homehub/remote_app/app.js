const $ = (id) => document.getElementById(id);
let shown = "";
let confirmId = null;
let current = null;
let systemTimer = null;
let restart = null;
let powerAction = null;
const TITLES = { main: "Home", chat: "Chat", ent: "Entertainment", consoles: "Consoles", computers: "Computers", household: "Around the house",
  adblock: "Ad blocking", vpn: "VPN devices", media: "Jellyfin", photos: "Photos", docs: "Documents", music: "Music", tv: "Sony TV", roku: "Roku", ps5: "PlayStation 5", proxmox: "Proxmox server", lists: "Lists", timers: "Timers & reminders",
  system: "Hub mini PC", apps: "Apps", packages: "Packages", mc: "Minecraft servers", mcnew: "New server", mcserver: "Server",
  router: "Router" };
const PARENT = { mcnew: "mc", mcserver: "mc", tv: "ent", roku: "ent", music: "ent", ps5: "consoles", system: "computers", proxmox: "computers",
  mc: "computers", apps: "computers", router: "computers", adblock: "computers", vpn: "computers", media: "ent", photos: "household", docs: "household", lists: "household", timers: "household", packages: "household" };
const VIEWS = Object.keys(TITLES);
const viewHooks = [];

function route() {
  const parts = location.hash.slice(1).split("/").filter(Boolean).map(decodeURIComponent);
  let view = parts[0] || "main";
  if (view === "mc" && parts[1] === "new") view = "mcnew";
  else if (view === "mc" && parts[1]) view = "mcserver";
  show(view, parts);
}

function show(view, parts = []) {
  if (!VIEWS.includes(view)) view = "main";
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === view));
  document.body.classList.toggle("chat-view", view === "chat");
  $("title").textContent = TITLES[view];
  $("back").hidden = view === "main";
  current = view;
  clearInterval(systemTimer);
  systemTimer = null;
  if (view === "system" || view === "main" || view === "computers") {
    refreshSystem();
    systemTimer = setInterval(refreshSystem, view === "system" ? 2000 : 5000);
  }
  if (view === "apps") refreshApps();
  if (view === "media" || view === "ent") refreshMedia();
  if (view === "media") refreshJellyfin();
  if (view === "photos") refreshPhotos();
  if (view === "docs") refreshDocs();
  if (view === "vpn" || view === "computers") refreshVpn();
  if (view !== "vpn") { hideVpnSecret(); $("vpn-confirm").hidden = true; }
  clearInterval(adTimer);
  adTimer = null;
  if (["system", "adblock", "computers"].includes(view)) refreshHub();
  if (view === "adblock" || view === "system") adTimer = setInterval(refreshHub, view === "adblock" ? 5000 : 10000);
  if (view !== "system" && view !== "adblock") $("hub-confirm").hidden = true;
  clearInterval(rokuTimer);
  rokuTimer = null;
  if (view === "roku") {
    refreshRoku();
    if ((features.devices || []).includes("roku")) rokuTimer = setInterval(refreshRoku, 5000);
  }
  clearInterval(tvTimer);
  tvTimer = null;
  if (view === "tv" && (features.devices || []).includes("tv")) {
    refreshTv();
    tvTimer = setInterval(refreshTv, 5000);
  }
  if ((view === "consoles" || view === "ps5" || view === "main") && controlsNow.ps5) refreshPs5();
  if (view === "main") refreshWeather();
  clearInterval(psnTimer);
  psnTimer = null;
  if (view === "ps5") {
    refreshPsn();
    psnTimer = setInterval(refreshPsn, 10000);
  }
  clearInterval(pxTimer);
  pxTimer = null;
  if ((view === "computers" || view === "proxmox" || view === "main") && controlsNow.proxmox) refreshProxmox();
  if (view === "proxmox") {
    pxTimer = setInterval(refreshProxmox, 5000);
    if (controlsNow.proxmox) updateWatch();
  }
  if (view !== "proxmox") $("px-confirm").hidden = true;
  viewHooks.forEach((hook) => hook(view, parts));
  if (view === "music" || view === "main") musicLoaded = false;
  homeKey = "";
  document.querySelector("main").scrollTop = 0;
  refresh();
}

const SUGGESTIONS = ["What's the weather today?", "Set a timer for 10 minutes", "What's on my shopping list?", "How's the internet?",
  "Turn on the TV", "What's in the news?"];

function renderLines(lines) {
  const key = JSON.stringify(lines);
  if (key === shown) return;
  shown = key;
  const list = $("lines");
  if (!lines.length) {
    const p = document.createElement("li");
    p.className = "info welcome";
    p.textContent = "Ask or tell the hub anything. Try one:";
    const chips = document.createElement("li");
    chips.className = "info suggestions";
    SUGGESTIONS.forEach((text) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = text;
      b.addEventListener("click", async () => {
        if (!(await post("/api/chat", { text }))) toast("Not sent: the hub isn't answering.");
        refresh();
      });
      chips.append(b);
    });
    list.replaceChildren(p, chips);
    return;
  }
  list.replaceChildren(...lines.map((l) => {
    const li = document.createElement("li");
    li.className = l.kind;
    li.textContent = l.text;
    if (l.kind === "you" || l.kind === "claude") {
      const t = document.createElement("time");
      t.textContent = l.at;
      li.append(t);
    }
    return li;
  }));
  const main = document.querySelector("main");
  main.scrollTop = main.scrollHeight;
}

let homeKey = "";
let musicLoaded = false;
let controlsNow = {};
let features = {};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function setDot(id, state) {
  $(id).className = `dot ${state || ""}`;
}
let toastTimer = null;

function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = !text;
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}

async function control(action, body = {}) {
  try {
    const r = await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ...body }) });
    const out = await r.json().catch(() => ({ ok: false, text: `Error ${r.status}` }));
    return out;
  } catch (e) {
    return { ok: false, text: "The hub isn't answering." };
  }
}

function when(item) {
  const due = new Date(item.due);
  const mins = Math.round((due - Date.now()) / 60000);
  const clock = due.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day = due.toDateString() === new Date().toDateString() ? "" : `${due.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}, `;
  const left = mins <= 0 ? "now" : mins < 60 ? `in ${mins} min` : mins < 1440 ? `in ${Math.floor(mins / 60)} h ${mins % 60} min` : "";
  return [left, `${day}${clock}`, item.repeat ? `repeats ${item.repeat}` : ""].filter(Boolean).join(" · ");
}

let selectedList = "shopping";
let person = "";

const KIND_ICON = { timer: "timer", reminder: "bell", alarm: "alarm" };

function greeting() {
  const h = new Date().getHours();
  const part = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  return person ? `${part}, ${person[0].toUpperCase()}${person.slice(1)}` : part;
}

function chip(text, tone) {
  const c = document.createElement("span");
  c.className = `chip ${tone || ""}`;
  c.textContent = text;
  return c;
}

let weatherNow = null;
let weatherAt = 0;
let lastHome = null;
const WEATHER_MS = 15 * 60 * 1000;

function parseWeather(text) {
  const now = /right now: (-?\d+)°([FC]) \(feels like (-?\d+)\), ([^,]+), wind/.exec(text);
  if (!now) return null;
  const day = /^Today \([^)]*\): [^,]+, high (-?\d+), low (-?\d+), chance of rain (\d+|unknown)%/m.exec(text);
  return { temp: now[1], unit: now[2], feels: now[3], sky: now[4], high: day ? day[1] : null, low: day ? day[2] : null,
    rain: day && day[3] !== "unknown" ? Number(day[3]) : null };
}

async function refreshWeather() {
  if (!controlsNow.weather || Date.now() - weatherAt < WEATHER_MS) return;
  weatherAt = Date.now();
  const out = await control("weather.now");
  const found = out.ok ? parseWeather(out.text || "") : null;
  if (found) weatherNow = { ...found, at: Date.now() };
  else weatherAt = Date.now() - WEATHER_MS + 60000;
  if (weatherNow && Date.now() - weatherNow.at > 4 * WEATHER_MS) weatherNow = null;
  if (lastHome) renderHero(lastHome);
}

function weatherChip(w) {
  const wet = /rain|drizzle|shower|thunder|snow|sleet/i.test(w.sky) || (w.rain != null && w.rain >= 60);
  const clear = /^(clear|mostly clear)$/i.test(w.sky);
  const parts = [`${w.temp}° ${w.sky[0].toUpperCase()}${w.sky.slice(1)}`];
  if (w.high != null) parts.push(`${w.high}°/${w.low}°`);
  if (w.rain != null && w.rain >= 30) parts.push(`${w.rain}% rain`);
  const c = chip(parts.join(" · "), `weather ${clear ? "sun" : wet ? "wet" : ""}`);
  c.prepend(icon(clear ? "sun" : wet ? "rain" : "cloud"));
  c.title = [`Feels like ${w.feels}°${w.unit}`, w.high != null ? `today ${w.high}° high, ${w.low}° low` : "",
    w.rain != null ? `${w.rain}% chance of rain` : ""].filter(Boolean).join(" · ");
  return c;
}

function renderGreeting() {
  $("hello").textContent = greeting();
  $("today").textContent = new Date().toLocaleString([], { weekday: "long", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderHero(home) {
  renderGreeting();
  const timers = home.timers || [];
  const chips = weatherNow ? [weatherChip(weatherNow)] : [];
  if (timers.length) {
    const next = timers[0];
    const c = chip(`${next.label || next.kind} ${when(next).split(" · ")[0]}`);
    c.prepend(icon(KIND_ICON[next.kind] || "timer"));
    chips.push(c);
  }
  Object.entries(home.lists || {}).forEach(([name, items]) => { if (items.length) chips.push(chip(`${items.length} on ${name}`)); });
  $("chips").replaceChildren(...chips);
}

function renderHome(home, controls) {
  controls = controls || {};
  lastHome = home;
  const firstControls = !controlsNow.known;
  controlsNow = { ...controls, known: true };
  $("bubble-tv").hidden = !controls.tv;
  $("bubble-roku").hidden = !controls.roku;
  $("bubble-music").hidden = !controls.music;
  $("bubble-lists").hidden = !controls.lists;
  $("bubble-timers").hidden = !controls.timers;
  $("bubble-ps5").hidden = !controls.ps5;
  $("bubble-proxmox").hidden = !controls.proxmox;
  $("tile-consoles").hidden = !controls.ps5;
  const ent = [controls.tv && "Sony TV", controls.roku && "Roku", controls.music && "Music"].filter(Boolean);
  $("sub-ent").textContent = ent.length ? ent.join(" · ") : "Nothing set up yet";
  $("ent-empty").hidden = ent.length > 0;
  $("sub-computers").textContent = ["Hub", controls.proxmox && "Proxmox", features.minecraft && "Minecraft"].filter(Boolean).join(" · ");
  if (firstControls && current) {
    if (controls.ps5 && ["consoles", "ps5", "main"].includes(current)) refreshPs5();
    if (controls.proxmox && ["computers", "proxmox", "main"].includes(current)) refreshProxmox();
    if (current === "main") refreshWeather();
  }
  if (controls.music && !musicLoaded && (current === "music" || current === "main")) {
    musicLoaded = true;
    refreshMusic();
  }
  const key = JSON.stringify([home.lists, home.timers, Math.floor(Date.now() / 30000), controls, selectedList]);
  if (key === homeKey) return;
  homeKey = key;
  renderHero(home);
  const timers = home.timers || [];
  $("sub-timers").textContent = timers.length ? `${timers.length} set · next ${when(timers[0]).split(" · ")[0]}` : "None set";
  const counts = Object.entries(home.lists || {}).filter(([, items]) => items.length);
  $("sub-lists").textContent = counts.length ? counts.map(([n, items]) => `${items.length} on ${n}`).join(", ") : "All clear";
  const listed = counts.reduce((n, [, items]) => n + items.length, 0);
  $("sub-household").textContent = [timers.length ? `${timers.length} timer${timers.length === 1 ? "" : "s"}` : "", listed ? `${listed} on lists` : ""]
    .filter(Boolean).join(" · ") || "Lists, timers, packages";
  if (!timers.length) {
    const li = document.createElement("li");
    li.className = "empty-row";
    li.textContent = "Nothing scheduled. Try \"remind me at 6 to call Mom\" in Chat.";
    $("timers-list").replaceChildren(li);
  } else {
    $("timers-list").replaceChildren(...timers.map((t) => {
      const li = document.createElement("li");
      const icon = document.createElement("span");
      icon.className = "badge";
      icon.append(window.icon(KIND_ICON[t.kind] || "timer"));
      const text = document.createElement("span");
      text.className = "grow";
      text.textContent = t.label ? `${t.label[0].toUpperCase()}${t.label.slice(1)}` : `${t.kind[0].toUpperCase()}${t.kind.slice(1)}`;
      const small = document.createElement("small");
      small.textContent = when(t);
      text.append(small);
      const cancel = document.createElement("button");
      cancel.className = "ghost icon-btn";
      cancel.append(window.icon("x"));
      cancel.setAttribute("aria-label", `Cancel ${t.label || t.kind}`);
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        const out = await control("timer.cancel", { id: t.id });
        toast(out.ok ? "Cancelled." : out.text);
        refresh();
      });
      li.append(icon, text, cancel);
      return li;
    }));
  }
  const lists = home.lists || {};
  const names = Object.keys(lists);
  if (!names.includes(selectedList)) names.unshift(selectedList);
  $("list-tabs").replaceChildren(...names.map((name) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.className = name === selectedList ? "active" : "";
    b.setAttribute("aria-selected", name === selectedList);
    const count = (lists[name] || []).length;
    b.textContent = `${name[0].toUpperCase()}${name.slice(1)}${count ? ` · ${count}` : ""}`;
    b.addEventListener("click", () => { selectedList = name; homeKey = ""; refresh(); });
    return b;
  }));
  const items = lists[selectedList] || [];
  $("add-text").placeholder = `Add to ${selectedList}`;
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty-row";
    li.textContent = `The ${selectedList} list is empty.`;
    $("list-items").replaceChildren(li);
  } else {
    $("list-items").replaceChildren(...items.map((i) => {
      const li = document.createElement("li");
      const done = document.createElement("button");
      done.className = "check";
      done.setAttribute("aria-label", `Done: ${i}`);
      const text = document.createElement("span");
      text.className = "grow";
      text.textContent = i;
      done.addEventListener("click", async () => {
        done.disabled = true;
        li.classList.add("done");
        const out = await control("list.remove", { list: selectedList, item: i });
        if (!out.ok) { li.classList.remove("done"); toast(out.text); }
        refresh();
      });
      li.append(done, text);
      return li;
    }));
  }
}

async function refreshMusic() {
  const out = await control("music.now");
  $("now-playing").textContent = out.text || (out.ok ? "Nothing playing" : "Spotify isn't answering");
  $("sub-music").textContent = !out.ok ? "Spotify" : /nothing is playing/i.test(out.text || "") ? "Nothing playing" : (out.text || "").split(/[.\n]/)[0];
  const volume = out.ok && /(\d{1,3})% volume/.exec(out.text || "");
  if (volume) $("music-volume").value = Math.min(100, Number(volume[1]));
}

function powerState(text, ok) {
  if (!ok) return "unknown";
  if (/rest mode/i.test(text)) return "rest";
  if (/ is off\b|already off|shutting down/i.test(text)) return "off";
  if (/ is on\b|already on/i.test(text)) return "on";
  return "unknown";
}

const SHORT = { on: "On", rest: "Rest mode", off: "Off", unknown: "Not answering" };

async function refreshPs5() {
  $("ps5-state").textContent = "Checking…";
  const out = await control("ps5.now");
  const state = powerState(out.text || "", out.ok);
  $("ps5-state").textContent = out.text || SHORT[state];
  setDot("ps5-dot", state);
  document.querySelectorAll("[data-ps5-power]").forEach((b) => {
    b.disabled = (b.dataset.ps5Power === "1" && state === "on") || (b.dataset.ps5Power === "0" && state === "rest");
    b.classList.toggle("primary", b.dataset.ps5Power === (state === "on" ? "0" : "1"));
  });
  setDot("dot-ps5", state);
  const playing = state === "on" && /running (.+?)\.?$/i.exec(out.text || "");
  $("sub-ps5").textContent = playing ? `On · ${playing[1]}` : SHORT[state];
  $("sub-consoles").textContent = `PS5 · ${SHORT[state].toLowerCase()}`;
}

let pxView = null;
let pxPending = null;
let pxOpen = null;
let pxTimer = null;
let pxBusy = false;
let pxAllTasks = false;
const PX_TASKS_SHOWN = 5;
const GUEST_WORDS = { start: "Start", shutdown: "Shut down", reboot: "Restart", stop: "Force stop", reset: "Hard reset" };
const TASK_NAMES = { vzstart: "Started container", vzshutdown: "Shut down container", vzstop: "Force-stopped container", vzreboot: "Restarted container",
  qmstart: "Started VM", qmshutdown: "Shut down VM", qmstop: "Force-stopped VM", qmreset: "Hard-reset VM", qmreboot: "Restarted VM", qmsuspend: "Paused VM",
  qmresume: "Resumed VM", aptupdate: "Checked for updates", startall: "Started guests after boot", stopall: "Stopped guests for shutdown",
  vzcreate: "Created container", qmcreate: "Created VM", vzdestroy: "Deleted container", qmdestroy: "Deleted VM", download: "Downloaded a template",
  srvreload: "Reloaded a service", srvrestart: "Restarted a service" };

function gib(n) { return n < 2 ** 30 ? `${Math.round(n / 2 ** 20)} MB` : `${(n / 2 ** 30).toFixed(n >= 100 * 2 ** 30 ? 0 : 1)} GB`; }

function pair(used, total) { return total < 2 ** 30 ? `${gib(used)} / ${gib(total)}` : `${(used / 2 ** 30).toFixed(1)} / ${(total / 2 ** 30).toFixed(total >= 100 * 2 ** 30 ? 0 : 1)} GB`; }

function pxTemp(c) { return c == null ? "–" : `${Math.round(c * 9 / 5 + 32)}°F`; }

async function devApi(name, body) {
  try {
    const r = await fetch(`/api/dev/${name}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({ error: `Error ${r.status}` }));
    return r.ok ? { ok: true, ...out } : { ok: false, text: out.error || `Error ${r.status}` };
  } catch (e) {
    return { ok: false, text: "The hub isn't answering." };
  }
}

async function refreshProxmox() {
  if (pxBusy) return;
  pxBusy = true;
  try {
    const r = await fetch("/api/dev/proxmox", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    pxView = v;
    renderProxmox(v);
  } catch (e) {
    $("px-state").textContent = e.message || "Not answering";
    setDot("px-dot", "unknown");
    setDot("dot-proxmox", "unknown");
    $("sub-proxmox").textContent = "Not answering";
  } finally {
    pxBusy = false;
  }
}

function renderProxmox(v) {
  const on = v.on;
  const state = on ? "on" : "off";
  setDot("px-dot", state);
  setDot("dot-proxmox", state);
  document.querySelectorAll("[data-px-power]").forEach((b) => {
    b.hidden = (b.dataset.pxPower === "on") === on;
    b.classList.toggle("primary", b.dataset.pxPower === "on");
  });
  $("px-off-note").hidden = on;
  $("px-on").hidden = !on;
  if (!on) {
    $("px-state").textContent = "Off";
    $("sub-proxmox").textContent = "Off";
    return;
  }
  const n = v.node;
  const running = v.guests.filter((g) => g.status === "running").length;
  $("px-state").textContent = `On · up ${duration(n.uptime).split(" ").slice(0, 2).join(" ")}`;
  $("sub-proxmox").textContent = `On · ${v.guests.length ? `${running} of ${v.guests.length} guests running` : `up ${duration(n.uptime).split(" ").slice(0, 2).join(" ")}`}`;
  $("px-cpu").textContent = `${Math.round(n.cpu)}%`;
  $("px-cpu-note").textContent = [n.cpus ? `${n.cpus} threads` : "", n.load.length ? `load ${n.load.map((x) => x.toFixed(2)).join(" ")}` : "", "last hour"].filter(Boolean).join(" · ");
  const pts = v.history.map((p) => p.cpu);
  const top = Math.max(10, ...pts);
  $("px-spark-line").setAttribute("points", pts.map((c, i) => `${(i / Math.max(1, pts.length - 1)) * 120},${32 - (c / top) * 30}`).join(" "));
  const memPct = 100 * n.mem_used / n.mem_total;
  $("px-mem").textContent = pair(n.mem_used, n.mem_total);
  $("px-mem-bar").style.width = `${memPct}%`;
  $("px-mem-bar").classList.toggle("high", memPct >= 85);
  const diskPct = n.root_total ? 100 * n.root_used / n.root_total : 0;
  $("px-disk").textContent = n.root_total ? pair(n.root_used, n.root_total) : "–";
  $("px-disk-bar").style.width = `${diskPct}%`;
  $("px-up").textContent = duration(n.uptime);
  $("px-temp").textContent = pxTemp(n.cpu_temp);
  $("px-temp-note").textContent = n.disk_temp != null ? `SSD ${pxTemp(n.disk_temp)}` : "";
  renderGuests(v.guests);
  $("px-updates").textContent = v.updates ? `${v.updates} update${v.updates === 1 ? "" : "s"} waiting` : "Up to date";
  $("px-install").hidden = !v.updates;
  $("px-storage").replaceChildren(...[...v.storage].sort((a, b) => a.name.localeCompare(b.name)).map((st) => {
    const box = document.createElement("div");
    box.className = "store";
    const head = document.createElement("div");
    head.className = "store-head";
    const name = document.createElement("strong");
    name.textContent = st.name === "local-lvm" ? "VM & container disks (local-lvm)" : st.name === "local" ? "Templates, ISOs & backups (local)" : st.name;
    const size = document.createElement("span");
    size.textContent = `${gib(st.used)} of ${gib(st.total)}`;
    head.append(name, size);
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("span");
    fill.style.width = `${st.total ? 100 * st.used / st.total : 0}%`;
    bar.append(fill);
    box.append(head, bar);
    return box;
  }));
  const tasks = pxAllTasks ? v.tasks : v.tasks.slice(0, PX_TASKS_SHOWN);
  $("px-tasks").replaceChildren(...(v.tasks.length ? tasks.map((t) => {
    const li = document.createElement("li");
    const text = document.createElement("span");
    text.className = "grow";
    text.textContent = `${TASK_NAMES[t.type] || t.type}${t.id ? ` ${t.id}` : ""}`;
    const small = document.createElement("small");
    small.textContent = `${new Date(t.start * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })} · ${(t.user || "").split("!")[0]}`;
    text.append(small);
    const result = document.createElement("span");
    const ok = t.status === "OK";
    result.className = `s-note ${ok ? "task-ok" : t.status === "running" ? "" : "task-bad"}`;
    result.textContent = ok ? "OK" : t.status === "running" ? "Running" : "Failed";
    li.append(text, result);
    return li;
  }) : [Object.assign(document.createElement("li"), { className: "empty-row", textContent: "Nothing yet." })]));
  if (v.tasks.length > PX_TASKS_SHOWN) {
    const more = make("button", "ghost wide", pxAllTasks ? "Show fewer" : `Show all ${v.tasks.length}`);
    more.type = "button";
    more.addEventListener("click", () => { pxAllTasks = !pxAllTasks; renderProxmox(pxView); });
    const li = make("li", "more-row");
    li.append(more);
    $("px-tasks").append(li);
  }
  $("px-about").textContent = [n.model, n.version.replace(/^pve-manager\//, "Proxmox VE ").split("/")[0], n.kernel ? `kernel ${n.kernel}` : ""].filter(Boolean).join(" · ");
}

function renderGuests(guests) {
  if (!guests.length) {
    const p = document.createElement("p");
    p.className = "empty-note";
    p.textContent = "No virtual machines or containers yet. Once you make some in Proxmox, you can start and stop them here.";
    $("px-guests").replaceChildren(p);
    return;
  }
  $("px-guests").replaceChildren(...guests.map((g) => {
    const row = document.createElement("div");
    row.className = "row guest";
    const tile = document.createElement("span");
    tile.className = `r-icon ${g.kind === "qemu" ? "tone-sky" : "tone-teal"}`;
    tile.append(icon(g.kind === "qemu" ? "tv" : "cube"));
    const text = document.createElement("span");
    text.className = "r-text";
    const name = document.createElement("span");
    name.className = "r-name";
    name.textContent = `${g.name} `;
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = `${g.kind === "qemu" ? "VM" : "CT"} ${g.vmid}`;
    name.append(kind);
    const sub = document.createElement("span");
    sub.className = "r-sub";
    sub.textContent = g.status === "running"
      ? `Running · CPU ${g.cpu}% · ${gib(g.mem)} of ${gib(g.maxmem)} · up ${duration(g.uptime).split(" ").slice(0, 2).join(" ")}`
      : `${g.status[0].toUpperCase()}${g.status.slice(1)} · ${gib(g.maxmem)} memory`;
    text.append(name, sub);
    const dot = document.createElement("span");
    dot.className = `dot ${g.status === "running" ? "on" : "off"}`;
    row.append(tile, text, dot);
    const key = `${g.kind}${g.vmid}`;
    if (pxOpen === key) {
      const actions = document.createElement("div");
      actions.className = "guest-actions";
      const usable = g.actions.filter((a) => (g.status === "running") === (a !== "start"));
      usable.forEach((a) => {
        const b = document.createElement("button");
        b.textContent = GUEST_WORDS[a];
        if (a === "start") b.className = "primary";
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          if (a === "start") return guestAction(b, g, a);
          pxPending = { kind: "guest", guest: g, action: a, button: b };
          $("px-question").textContent = a === "stop" ? `Force stop ${g.name}? It turns off right away, like pulling the plug; unsaved work inside it is lost.`
            : a === "reset" ? `Hard reset ${g.name}? It restarts right away without shutting down; unsaved work inside it is lost.`
            : `${GUEST_WORDS[a]} ${g.name}?`;
          $("px-yes").textContent = `Yes, ${GUEST_WORDS[a].toLowerCase()}`;
          $("px-confirm").hidden = false;
          $("px-confirm").scrollIntoView({ behavior: "smooth", block: "center" });
        });
        actions.append(b);
      });
      row.append(actions);
    }
    row.addEventListener("click", () => { pxOpen = pxOpen === key ? null : key; renderGuests(pxView.guests); });
    return row;
  }));
}

async function guestAction(button, g, action) {
  button.disabled = true;
  toast(`${GUEST_WORDS[action]}: ${g.name}…`);
  const out = await devApi("proxmox", { action: "guest", vmid: g.vmid, do: action });
  button.disabled = false;
  toast(out.text);
  refreshProxmox();
}

let rokuView = null;
let rokuIcons = {};
let rokuTimer = null;
let rokuBusy = false;

function iconImage(src) {
  const img = document.createElement("img");
  img.src = src;
  img.alt = "";
  img.loading = "lazy";
  return img;
}

async function refreshRoku() {
  if (!(features.devices || []).includes("roku")) {
    const out = await control("roku.now");
    $("roku-state").textContent = out.text || "The Roku isn't answering.";
    return;
  }
  if (rokuBusy) return;
  rokuBusy = true;
  try {
    const r = await fetch("/api/dev/roku", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    rokuView = v;
    renderRoku(v);
    if (v.apps.some((a) => !rokuIcons[a.id])) {
      const out = await devApi("roku", { action: "icons" });
      if (out.ok) { rokuIcons = out.icons; renderRoku(rokuView, true); }
    }
  } catch (e) {
    $("roku-state").textContent = e.message || "Not answering";
    setDot("roku-dot", "unknown");
  } finally {
    rokuBusy = false;
  }
}

function renderRoku(v, force) {
  $("roku-name").textContent = v.name;
  setDot("roku-dot", v.on ? "on" : "off");
  $("roku-state").textContent = !v.on ? v.power[0].toUpperCase() + v.power.slice(1)
    : v.screensaver ? `Screensaver over ${v.active ? v.active.title : "the home screen"}` : v.active ? `On · ${v.active.title}` : "On · home screen";
  $("roku-notice").hidden = !v.limited;
  $("roku-notice").textContent = v.notice || "";
  $("roku-now").hidden = !v.active;
  if (v.active) {
    $("roku-now-icon").src = rokuIcons[v.active.id] || "";
    $("roku-now-icon").hidden = !rokuIcons[v.active.id];
    $("roku-now-title").textContent = v.active.title;
    const p = v.player;
    $("roku-now-time").textContent = p ? `${p.state === "pause" ? "Paused" : "Playing"}${p.position != null ? ` · ${clockText(p.position)}${p.duration ? ` of ${clockText(p.duration)}` : ""}` : ""}` : "Open";
    $("roku-now-bar-box").hidden = !(p && p.duration);
    if (p && p.duration) $("roku-now-bar").style.width = `${Math.min(100, 100 * p.position / p.duration)}%`;
  }
  const key = JSON.stringify([v.apps.map((a) => a.id), Object.keys(rokuIcons).length, v.active && v.active.id]);
  if (!force && $("roku-apps").dataset.key === key) return;
  $("roku-apps").dataset.key = key;
  $("roku-apps").replaceChildren(...v.apps.map((a) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `app-tile${v.active && v.active.id === a.id ? " active" : ""}`;
    if (rokuIcons[a.id]) b.append(iconImage(rokuIcons[a.id]));
    else b.append(Object.assign(document.createElement("span"), { className: "ph", textContent: a.title[0] }));
    b.append(document.createTextNode(a.title));
    b.addEventListener("click", async () => {
      b.disabled = true;
      const out = await devApi("roku", { action: "launch", id: a.id });
      b.disabled = false;
      toast(out.text);
      setTimeout(refreshRoku, 2500);
    });
    return b;
  }));
}

$("roku-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("roku-search-text").value.trim();
  if (!text) return;
  const out = await devApi("roku", { action: "search", text });
  toast(out.text);
  if (out.ok) $("roku-search-text").value = "";
});

function clockText(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

let misses = 0;
let featuresLoading = false;
let refreshing = false;
const STATE_WAIT_MS = 6000;
let everConnected = false;
const OFFLINE_TEXT = "This phone can't reach the hub right now. Turn on the WireGuard VPN, or check that the hub has power and internet. The app reconnects by itself.";

function connection(ok, detail) {
  if (ok) {
    misses = 0;
    everConnected = true;
    $("offline").hidden = true;
    $("hub").textContent = "Connected";
    $("hub").className = "pill on";
    if (!features.known && !featuresLoading) {
      featuresLoading = true;
      loadFeatures().then(route).catch(() => {}).finally(() => { featuresLoading = false; });
    }
    return;
  }
  misses += 1;
  if (!lastHome) renderGreeting();
  $("hub").textContent = restart ? (restart.action === "off" ? "Off" : "Restarting") : "Offline";
  $("hub").className = "pill bad";
  if ((misses >= 2 || !everConnected) && !restart) {
    $("offline-detail").textContent = detail || OFFLINE_TEXT;
    $("offline").hidden = false;
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  let s;
  try {
    const r = await fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout ? AbortSignal.timeout(STATE_WAIT_MS) : undefined });
    if (!r.ok) {
      connection(false, r.status === 403 ? "This device isn't one of the household's VPN devices, so the hub won't answer it."
        : PROXY_DOWN_CODES.includes(r.status) ? "The hub's address answers, but the app behind it doesn't. It may be restarting; this page reconnects by itself."
        : `The hub answered with error ${r.status}. This page keeps trying.`);
      return;
    }
    s = await r.json();
  } catch (e) {
    connection(false, "");
    return;
  } finally {
    refreshing = false;
  }
  connection(true);
  person = s.person || "";
  renderLines(s.chat.lines);
  renderHome(s.home, s.controls);
  $("working").hidden = !s.chat.busy || !!s.chat.confirm;
  $("sub-chat").textContent = s.chat.confirm ? "Needs your answer" : s.chat.busy ? "Working on it…" : s.chat.claude_down ? "Claude unavailable" : "Ask or command";
  $("claude-down").hidden = !s.chat.claude_down;
  $("claude-down").textContent = s.chat.claude_down ? `Claude is unavailable: ${s.chat.claude_down} Quick commands like timers, alarms and lists still work here, and so does the rest of the app.` : "";
  const c = s.chat.confirm;
  $("confirm").hidden = !c;
  if (c && c.id !== confirmId) {
    confirmId = c.id;
    $("confirm-text").textContent = `OK to ${c.summary}?`;
    $("yes").disabled = $("no").disabled = false;
  }
  if (!c) confirmId = null;
}

const PROXY_DOWN_CODES = [502, 503, 504];

function failText(e) {
  if (e instanceof SyntaxError) return "The hub sent an answer this app couldn't read.";
  return e instanceof TypeError || !e.message ? "The hub isn't answering." : e.message;
}

$("offline-retry").addEventListener("click", async () => {
  $("offline-retry").disabled = true;
  $("offline-retry").textContent = "Trying…";
  misses = Math.max(misses, 1);
  await refresh();
  $("offline-retry").disabled = false;
  $("offline-retry").textContent = "Retry";
});

async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return r.ok;
  } catch (e) {
    return false;
  }
}

let sending = false;
$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || sending) return;
  sending = true;
  const ok = await post("/api/chat", { text });
  sending = false;
  if (ok) {
    if ($("text").value.trim() === text) $("text").value = "";
  } else {
    if (!$("text").value.trim()) $("text").value = text;
    toast("Not sent: the hub isn't answering. It's still in the box.");
  }
  refresh();
});

async function answer(yes) {
  if (!confirmId) return;
  $("yes").disabled = $("no").disabled = true;
  if (!(await post("/api/confirm", { id: confirmId, yes }))) {
    $("yes").disabled = $("no").disabled = false;
    toast("The answer didn't reach the hub. Try again.");
  }
  refresh();
}
function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
}

function bits(perSecond) {
  if (perSecond == null) return "–";
  const b = perSecond * 8;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} Mbps`;
  return `${Math.round(b / 1e3)} kbps`;
}

function duration(seconds) {
  seconds = Math.max(0, seconds || 0);
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  return `${m} min ${Math.floor(seconds % 60)} s`;
}

function meter(id, percent, text, more) {
  $(id).textContent = text;
  $(`${id}-bar`).style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
  $(`${id}-bar`).classList.toggle("high", (percent || 0) >= 85);
  $(`${id}-more`).textContent = more;
}

const temp = (c) => (c == null ? "" : `${Math.round(c * 9 / 5 + 32)}°F`);
const NET_NAMES = { wlo1: "Wi-Fi", wg0: "VPN", enp1s0: "Ethernet", eth0: "Ethernet" };

function renderSystem(s) {
  $("sub-system").textContent = `CPU ${s.cpu.percent == null ? "…" : Math.round(s.cpu.percent) + "%"} · up ${duration(s.uptime_seconds).split(" ").slice(0, 2).join(" ")}`;
  setDot("dot-hub", "on");
  setDot("sys-dot", "on");
  $("uptime").textContent = `Up ${duration(s.uptime_seconds)}, since ${new Date(s.booted_at * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
  const a = s.assistant || {};
  $("assistant").textContent = a.ready_at
    ? `Assistant running for ${duration((s.now || Date.now() / 1000) - a.started_at)} · ${a.version || ""}`
    : "Assistant starting…";
  meter("cpu", s.cpu.percent, s.cpu.percent == null ? "…" : `${Math.round(s.cpu.percent)}%`,
    [`${s.cpu.cores} threads`, temp(s.cpu.temp_c)].filter(Boolean).join(" · "));
  if (s.gpu) {
    meter("gpu", s.gpu.percent, `${s.gpu.percent}%`,
      [s.gpu.memory_total ? `${bytes(s.gpu.memory_used)} of ${bytes(s.gpu.memory_total)}` : "", temp(s.gpu.temp_c)].filter(Boolean).join(" · "));
  } else {
    meter("gpu", 0, "–", "No GPU reading");
  }
  const memPct = s.memory.total ? 100 * s.memory.used / s.memory.total : 0;
  meter("mem", memPct, `${Math.round(memPct)}%`, `${bytes(s.memory.used)} of ${bytes(s.memory.total)}`);
  const diskPct = s.disk.total ? 100 * s.disk.used / s.disk.total : 0;
  meter("disk", diskPct, `${Math.round(diskPct)}%`, [`${bytes(s.disk.total - s.disk.used)} free of ${bytes(s.disk.total)}`, temp(s.disk.temp_c)].filter(Boolean).join(" · "));
  $("net").replaceChildren(...s.network.filter((n) => n.up).map((n) => {
    const row = document.createElement("div");
    row.className = "net";
    const name = document.createElement("strong");
    name.textContent = (NET_NAMES[n.name] || n.name) + (n.internet ? " (home internet)" : "");
    const live = document.createElement("p");
    live.textContent = `↓ ${bits(n.rx_rate)}   ↑ ${bits(n.tx_rate)}` + (n.signal_dbm != null ? `   · signal ${n.signal_dbm} dBm` : "");
    const total = document.createElement("p");
    total.className = "muted";
    total.textContent = `Since start: ${bytes(n.rx_total)} in, ${bytes(n.tx_total)} out`;
    row.append(name, live, total);
    return row;
  }));
  $("power-card").hidden = !s.power;
  const checks = Object.entries(a.checks || {}).filter(([k]) => k !== "models");
  $("checks-card").hidden = !checks.length;
  $("checks").replaceChildren(...checks.map(([k, v]) => {
    const li = document.createElement("li");
    li.textContent = `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`;
    return li;
  }));
}

function showRestart(title, text) {
  $("restarting").hidden = !title;
  $("restart-title").textContent = title || "";
  $("restart-text").textContent = text || "";
}

async function refreshSystem() {
  const elapsed = restart ? Math.round((Date.now() - restart.at) / 1000) : 0;
  try {
    const r = await fetch("/api/system", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    $("hub").textContent = "Connected";
    $("hub").className = "pill on";
    renderSystem(s);
    if (restart) {
      const back = restart.action === "assistant" ? (s.assistant || {}).started_at > restart.started : s.booted_at > restart.booted;
      if (back && (s.assistant || {}).ready_at) {
        showRestart("Back up and running", restart.action === "off" ? "The mini PC is back on and everything below started again."
          : `${restart.action === "hub" ? "The mini PC" : "The assistant"} restarted in ${elapsed} s and everything below started again.`);
        restart = null;
      } else if (back) {
        showRestart("Starting up…", `The assistant is loading (${elapsed} s).`);
      } else {
        showRestart(restart.action === "off" ? "Shutting down the mini PC…" : restart.action === "hub" ? "Restarting the mini PC…" : "Restarting the assistant…", `Shutting down (${elapsed} s).`);
      }
    }
  } catch (e) {
    $("hub").textContent = restart ? (restart.action === "off" ? "Off" : "Restarting") : "Offline";
    $("hub").className = "pill bad";
    setDot("sys-dot", "unknown");
    setDot("dot-hub", "unknown");
    if (!restart && $("uptime").textContent === "Checking…") $("uptime").textContent = "Not answering";
    if (restart && restart.action === "off") {
      showRestart("The mini PC is off", restart.wake
        ? `It turns itself back on ${new Date(restart.wake * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}. This page updates by itself once it's back.`
        : "Press its power button to turn it back on. This page updates by itself once it's back.");
    } else if (restart) {
      showRestart(restart.action === "hub" ? "Restarting the mini PC…" : "Restarting the assistant…",
        elapsed > 240 ? `Still not back after ${elapsed} s. It may need a look in person.` : `Offline while it starts up (${elapsed} s). This page updates by itself.`);
    }
  }
}

const APP_LOGOS = [["immich", "immich", "#4250AF"], ["paperless", "paperlessngx", "#17541F"], ["cockpit", "cockpit", "#0066CC"],
  ["syncthing", "syncthing", "#0891D1"], ["adguard", "adguard", "#68BC71"], ["jellyfin", "jellyfin", "#00A4DC"], ["proxmox", "proxmox", "#E57000"]];

function appIcon(name) {
  const tile = document.createElement("span");
  const match = APP_LOGOS.find(([key]) => name.toLowerCase().includes(key));
  if (match) {
    tile.className = "r-icon logo-tile";
    tile.style.setProperty("--b", match[2]);
    if (match[3]) tile.style.setProperty("--f", match[3]);
    const svg = icon(`x`);
    svg.classList.add("logo");
    if (match[4]) svg.classList.add("full");
    svg.firstChild.setAttribute("href", `#b-${match[1]}`);
    tile.append(svg);
  } else {
    tile.className = "r-icon tone-pink";
    tile.append(icon("grid"));
  }
  return tile;
}

let hubView = null;
let hubBusy = false;
let hubPending = null;
let adTimer = null;

async function refreshHub() {
  if (hubBusy || !(features.devices || []).includes("hub")) return;
  hubBusy = true;
  try {
    const r = await fetch("/api/dev/hub", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    hubView = v;
    $("hub-error").hidden = true;
    renderHub(v);
  } catch (e) {
    const text = failText(e);
    $("ag-state").textContent = text;
    setDot("ag-dot", "unknown");
    $("hub-error").textContent = `Couldn't load services, Bluetooth and backups: ${text}`;
    $("hub-error").hidden = false;
    agUnavailable(text);
  } finally {
    hubBusy = false;
  }
}

function make(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function rowButton(text, onClick, primary) {
  const b = make("button", `row-btn${primary ? " primary" : ""}`, text);
  b.type = "button";
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(b); });
  return b;
}

function askHub(question, yes, job) {
  hubPending = job;
  $("hub-question").textContent = question;
  $("hub-yes").textContent = yes;
  $("hub-confirm").hidden = false;
  $("hub-confirm").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function hubDo(button, body, after) {
  if (button) button.disabled = true;
  const out = await devApi("hub", body);
  if (button) button.disabled = false;
  toast(out.text);
  refreshHub();
  if (after) setTimeout(refreshHub, after);
  return out;
}

function niceTime(text) {
  if (!text) return "";
  const when = new Date(text.replace(/ ([A-Z]{3,4})$/, ""));
  return Number.isNaN(when.getTime()) ? text : when.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

function renderHub(v) {
  $("hub-extra").hidden = false;
  $("hub-services").replaceChildren(...v.services.map((s) => {
    const row = make("div", "row");
    const text = make("span", "r-text");
    text.append(make("span", "r-name", s.name), make("span", "r-sub", s.state === "running" ? "Running" : s.detail));
    row.append(make("span", `dot ${s.state === "running" ? "on" : s.state === "stopped" ? "off" : "rest"}`), text,
      rowButton("Restart", (b) => askHub(`Restart ${s.name}? It's unavailable for about half a minute${s.id === "adguard" ? "; the house uses the backup DNS meanwhile" : ""}.`,
        "Yes, restart", { body: { action: "restart", id: s.id }, button: b })));
    return row;
  }), ...(v.syncthing ? [(() => {
    const row = make("div", "row");
    const text = make("span", "r-text");
    text.append(make("span", "r-name", "Syncthing file sync"), make("span", "r-sub", v.syncthing === "active" ? "Running" : v.syncthing));
    row.append(make("span", `dot ${v.syncthing === "active" ? "on" : "off"}`), text);
    return row;
  })()] : []));
  $("hub-bt-box").hidden = !v.bluetooth || !v.bluetooth.length;
  $("hub-bt").replaceChildren(...(v.bluetooth || []).map((d) => {
    const row = make("div", "row");
    const text = make("span", "r-text");
    text.append(make("span", "r-name", d.name), make("span", "r-sub", d.connected ? `Connected${d.kind === "phone" ? " · calls go through the hub" : ""}` : "Paired, not connected"));
    row.append(make("span", `dot ${d.connected ? "on" : "off"}`), text,
      rowButton(d.connected ? "Disconnect" : "Connect", (b) => hubDo(b, { action: d.connected ? "bt_disconnect" : "bt_connect", mac: d.mac }), !d.connected));
    return row;
  }));
  $("hub-backups").replaceChildren(...v.backups.map((b) => {
    const row = make("div", "row");
    const text = make("span", "r-text");
    const last = b.last.replace(/^last run /, "Last: ");
    text.append(make("span", "r-name", b.name), make("span", "r-sub", b.running ? "Backing up now…" : `${last}${b.next ? ` · next ${niceTime(b.next)}` : ""}`));
    row.append(make("span", `dot ${b.running ? "rest" : /worked/.test(b.last) ? "on" : "off"}`), text,
      rowButton("Back up now", (btn) => hubDo(btn, { action: "backup", id: b.id }, 4000), false));
    return row;
  }));
  renderAdblock(v);
}

function renderAdblock(v) {
  const ag = v.adguard;
  $("bubble-adblock").hidden = !ag && !v.skipper;
  if (ag && !ag.error) {
    const paused = !ag.protection;
    setDot("ag-dot", paused ? "rest" : "on");
    setDot("dot-adblock", paused ? "rest" : "on");
    $("ag-state").textContent = paused ? (ag.paused_ms ? `Paused · back on in ${Math.max(1, Math.round(ag.paused_ms / 60000))} min` : "Off") : "On · blocking ads and trackers";
    $("sub-adblock").textContent = paused ? "Paused" : `${ag.blocked.toLocaleString()} blocked today (${ag.percent}%)`;
    $("ag-pause-row").hidden = paused;
    $("ag-resume-row").hidden = !paused;
    $("ag-queries").textContent = ag.queries.toLocaleString();
    $("ag-blocked").textContent = ag.blocked.toLocaleString();
    $("ag-percent").textContent = `${ag.percent}% of lookups`;
    $("ag-recent").replaceChildren(...(ag.recent_blocked.length ? ag.recent_blocked.map((r) => {
      const li = make("li");
      const text = make("span", "grow");
      text.append(make("span", "mono-dom", r.domain), make("small", null, `${r.client} · ${new Date(r.time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`));
      li.append(text, rowButton("Unblock", (b) => askHub(`Always allow ${r.domain} for the whole house?`, "Yes, allow", { body: { action: "adguard_allow", domain: r.domain }, button: b })));
      return li;
    }) : [make("li", "empty-row", "Nothing blocked recently.")]));
    const rules = [...ag.allowed.map((d) => ["allow", d]), ...ag.custom_blocked.map((d) => ["block", d])];
    $("ag-rules").replaceChildren(...(rules.length ? rules.map(([kind, d]) => {
      const li = make("li");
      const text = make("span", "grow");
      text.append(make("span", "mono-dom", d));
      li.append(make("span", `tag ${kind}`, kind === "allow" ? "Allowed" : "Blocked"), text,
        rowButton("Remove", (b) => askHub(`Remove your rule for ${d}? The blocklists decide again.`, "Yes, remove", { body: { action: "adguard_remove", domain: d }, button: b })));
      return li;
    }) : [make("li", "empty-row", "No rules of your own yet.")]));
    $("ag-top").replaceChildren(...ag.top_blocked.map((d) => {
      const li = make("li");
      const text = make("span", "grow");
      text.append(make("span", "mono-dom", d.domain));
      li.append(text, make("span", "s-note", d.count.toLocaleString()));
      return li;
    }));
  } else if (ag && ag.error) {
    $("ag-state").textContent = ag.error;
    setDot("ag-dot", "unknown");
    agUnavailable(ag.error);
  }
  $("sk-card").hidden = !v.skipper;
  if (v.skipper) {
    const what = [v.skipper.skip_ads ? "skips YouTube ads as soon as it can" : "", v.skipper.mute_ads ? "mutes them meanwhile" : "",
      v.skipper.skips.length ? `skips ${v.skipper.skips.map((s) => ({ sponsor: "sponsor segments", selfpromo: "self-promotion", interaction: "like-and-subscribe reminders", intro: "intros", outro: "end screens" }[s] || s)).join(", ")}` : ""].filter(Boolean);
    $("sk-what").textContent = `On each paired TV's YouTube app it ${what.join(", ")}.`;
    $("sk-devices").replaceChildren(...(v.skipper.devices.length ? v.skipper.devices.map((d) => {
      const li = make("li");
      li.append(make("span", "dot on"), make("span", "grow", d));
      return li;
    }) : [make("li", "empty-row", "No TVs paired yet.")]));
  }
}

function agUnavailable(text) {
  ["ag-recent", "ag-rules", "ag-top"].forEach((id) => {
    if (!$(id).querySelector("li:not(.empty-row)")) $(id).replaceChildren(make("li", "empty-row", `Unavailable: ${text}`));
  });
}

$("hub-no").addEventListener("click", () => { $("hub-confirm").hidden = true; hubPending = null; });
$("hub-yes").addEventListener("click", () => {
  $("hub-confirm").hidden = true;
  const job = hubPending;
  hubPending = null;
  if (job) hubDo(job.button, job.body, 3000);
});
$("ag-refresh").addEventListener("click", refreshHub);
document.querySelectorAll("[data-ag-pause]").forEach((b) => b.addEventListener("click", () => hubDo(b, { action: "adguard_pause", minutes: Number(b.dataset.agPause) })));
$("ag-add").addEventListener("submit", (e) => {
  e.preventDefault();
  const domain = $("ag-domain").value.trim();
  if (!domain) return;
  hubDo(null, { action: "adguard_allow", domain }).then((out) => { if (out.ok) $("ag-domain").value = ""; });
});
$("ag-block-btn").addEventListener("click", () => {
  const domain = $("ag-domain").value.trim();
  if (!domain) return;
  askHub(`Block ${domain} for the whole house?`, "Yes, block", { body: { action: "adguard_block", domain }, button: $("ag-block-btn") });
});
$("sk-pair").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("sk-go").disabled = true;
  $("sk-go").textContent = "Pairing…";
  const out = await hubDo(null, { action: "pair", code: $("sk-code").value, name: $("sk-name").value });
  $("sk-go").disabled = false;
  $("sk-go").textContent = "Pair TV";
  if (out.ok) { $("sk-code").value = ""; $("sk-name").value = ""; }
});

let vpnPending = null;

function seenText(d) {
  if (d.online) return "Connected now";
  if (!d.last_seen) return "Never connected";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - d.last_seen) / 60));
  return mins < 60 ? `Last seen ${mins} min ago` : mins < 1440 ? `Last seen ${Math.round(mins / 60)} h ago` : `Last seen ${Math.round(mins / 1440)} d ago`;
}

async function refreshVpn() {
  try {
    const r = await fetch("/api/dev/vpn", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    $("vpn-add-card").hidden = !v.can_manage || !$("vpn-new").hidden;
    $("vpn-note").textContent = v.can_manage ? "Removing a device cuts it off right away, for example a lost phone." : "Only the household's owner can add or remove devices.";
    $("vpn-list").replaceChildren(...v.devices.map((d) => {
      const row = make("div", "row");
      const text = make("span", "r-text");
      const name = make("span", "r-name", d.name);
      if (d.this_device) name.append(make("span", "badge-me", "This device"));
      text.append(name, make("span", "r-sub", `${d.ip}${d.person ? ` · ${d.person}` : ""} · ${seenText(d)}`));
      row.append(make("span", `dot ${d.online ? "on" : "off"}`), text);
      if (v.can_manage && !d.this_device && d.managed) {
        row.append(rowButton("Remove", () => {
          vpnPending = d.name;
          $("vpn-question").textContent = `Remove ${d.name}? It can't reach the hub or this app anymore until it's added again.`;
          $("vpn-confirm").hidden = false;
        }));
      }
      return row;
    }));
    if (!v.devices.length) $("vpn-list").replaceChildren(make("p", "empty-note", "No devices yet."));
    $("sub-vpn").textContent = `${v.devices.length} device${v.devices.length === 1 ? "" : "s"} · ${v.devices.filter((d) => d.online).length} connected`;
  } catch (e) {
    const text = failText(e);
    if (!$("vpn-list").querySelector(".row")) $("vpn-list").replaceChildren(make("p", "empty-note", text));
    else toast(text);
  }
}

$("vpn-no").addEventListener("click", () => { $("vpn-confirm").hidden = true; vpnPending = null; });
$("vpn-yes").addEventListener("click", async () => {
  $("vpn-confirm").hidden = true;
  const name = vpnPending;
  vpnPending = null;
  if (!name) return;
  const out = await devApi("vpn", { action: "remove", name });
  toast(out.text);
  refreshVpn();
});
$("vpn-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("vpn-name").value.trim();
  if (!name) return;
  const out = await devApi("vpn", { action: "add", name });
  if (!out.ok) { toast(out.text); return; }
  $("vpn-name").value = "";
  $("vpn-new-title").textContent = `Set up ${out.name} (${out.ip})`;
  $("vpn-qr").src = out.qr;
  $("vpn-conf").value = out.conf;
  $("vpn-new").hidden = false;
  $("vpn-add-card").hidden = true;
  refreshVpn();
});
$("vpn-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("vpn-conf").value); toast("Copied. Paste it only on the new device."); }
  catch (e) { $("vpn-conf").select(); toast("Select the text and copy it."); }
});
function hideVpnSecret() {
  $("vpn-new").hidden = true;
  $("vpn-qr").removeAttribute("src");
  $("vpn-conf").value = "";
}
$("vpn-done").addEventListener("click", () => { hideVpnSecret(); refreshVpn(); });

let psnImages = {};
let psnTimer = null;

function psnImg(url, cls) {
  if (url && psnImages[url]) {
    const img = iconImage(psnImages[url]);
    if (cls) img.className = cls;
    return img;
  }
  return null;
}

function agoText(iso) {
  if (!iso) return "";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  return mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;
}

async function refreshPsn() {
  if (!(features.devices || []).includes("psn")) return;
  $("psn-box").hidden = false;
  let v;
  try {
    const r = await fetch("/api/dev/psn", { cache: "no-store" });
    v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
  } catch (e) {
    $("psn-error").hidden = false;
    $("psn-error").textContent = e.message;
    $("psn-token-state").textContent = "The PlayStation sign-in needs renewing.";
    return;
  }
  $("psn-error").hidden = !v.error;
  $("psn-error").textContent = v.error || "";
  $("psn-loading").hidden = !v.loading;
  $("psn-live").hidden = !!v.loading;
  if (v.loading) return;
  if (Object.keys(psnImages).length === 0) {
    devApi("psn", { action: "images" }).then((out) => { if (out.ok) { psnImages = out.images; renderPsn(v); } });
  }
  renderPsn(v);
}

function renderPsn(v) {
  const me = v.me;
  const avatar = psnImages[v.avatar];
  $("psn-avatar").hidden = !avatar;
  if (avatar) $("psn-avatar").src = avatar;
  $("psn-name").textContent = v.online_id;
  $("psn-plus").hidden = !v.plus;
  setDot("psn-dot", me.online ? "on" : "off");
  $("psn-state").textContent = me.online ? `Online on ${me.platform}${me.game ? ` · ${me.game}` : ""}` : `Offline · last online ${agoText(me.last_online)}`;
  $("psn-now").hidden = !me.game;
  $("psn-now-game").textContent = me.game;
  const nowImg = psnImages[me.game_image];
  $("psn-now-img").hidden = !nowImg;
  if (nowImg) $("psn-now-img").src = nowImg;
  $("psn-friends").replaceChildren(...(v.friends.length ? v.friends.map((f) => {
    const row = make("div", "row");
    const text = make("span", "r-text");
    text.append(make("span", "r-name", f.name), make("span", "r-sub", f.online ? `Online${f.game ? ` · playing ${f.game}` : ""}` : `Last online ${agoText(f.last_online)}`));
    row.append(make("span", `dot ${f.online ? "on" : "off"}`), text);
    return row;
  }) : [make("p", "empty-note", "No friends on PlayStation Network yet.")]));
  $("psn-recent").replaceChildren(...v.recent.map((g) => {
    const box = make("div", "game");
    box.append(psnImg(g.image) || make("span", "ph", g.name[0]));
    const text = make("div");
    text.append(make("strong", null, g.name), make("small", null, `${g.time} · ${g.plays} session${g.plays === 1 ? "" : "s"}`), make("small", null, `Last played ${agoText(g.last)}`));
    box.append(text);
    return box;
  }));
  const t = v.trophies;
  $("psn-level").textContent = `Level ${t.level}`;
  $("psn-counts").replaceChildren(...["platinum", "gold", "silver", "bronze"].map((k) => make("span", `t-${k}`, `${t.earned[k]} ${k}`)));
  $("psn-level-bar").style.width = `${t.progress}%`;
  $("psn-level-note").textContent = `${t.progress}% of the way to level ${t.level + 1}`;
  $("psn-trophy-games").replaceChildren(...t.games.map((g) => {
    const li = make("li");
    const img = psnImg(g.image, "t-icon-img");
    const text = make("span", "grow");
    const bar = make("div", "mini-bar");
    const fill = make("span");
    fill.style.width = `${g.progress}%`;
    bar.append(fill);
    text.append(make("strong", null, g.name), bar, make("small", null, `${g.progress}% · ${g.earned.gold} gold, ${g.earned.silver} silver, ${g.earned.bronze} bronze`));
    if (img) li.append(img);
    li.append(text);
    return li;
  }));
  $("psn-library").replaceChildren(...v.library.map((g) => {
    const tile = make("div", "app-tile");
    tile.append(psnImg(g.image) || make("span", "ph", g.name[0]), document.createTextNode(g.name));
    return tile;
  }));
  const days = v.token_days_left;
  $("psn-token-state").textContent = days == null ? "Signed in to PlayStation Network." : days > 7 ? `Signed in to PlayStation Network for ${days} more days. You'll get a reminder a week before it runs out.`
    : `The PlayStation sign-in runs out in ${Math.max(0, days)} day${days === 1 ? "" : "s"}. Renew it below.`;
}

$("psn-token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const token = $("psn-token").value.trim();
  if (!token) return;
  const out = await devApi("psn", { action: "set_token", token });
  toast(out.text);
  if (out.ok) { $("psn-token").value = ""; psnImages = {}; setTimeout(refreshPsn, 3000); }
});
$("psn-refresh").addEventListener("click", async () => {
  const out = await devApi("psn", { action: "refresh" });
  toast(out.text);
  setTimeout(refreshPsn, 5000);
});

let mdKind = "movie";
let mdFiles = [];
let mdBusy = false;
const MD_VIDEO = /\.(mkv|mp4|m4v|avi|mov|webm|ts|m2ts|wmv|mpg|mpeg|srt|ass|ssa|vtt|sub|idx|jpg|jpeg|png|nfo)$/i;
const MD_AUDIO = /\.(mp3|flac|m4a|aac|ogg|opus|wav|alac|wma|jpg|jpeg|png)$/i;

function gbText(n) { return `${(n / 2 ** 30).toFixed(n >= 100 * 2 ** 30 ? 0 : 1)} GB`; }

async function refreshMedia() {
  if (!(features.devices || []).includes("media")) return;
  try {
    const r = await fetch("/api/dev/media", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    setDot("md-dot", "on");
    const total = v.libraries.reduce((n, l) => n + l.count, 0);
    $("md-state").textContent = `Ready · ${total} title${total === 1 ? "" : "s"}`;
    $("sub-media").textContent = `${v.libraries.map((l) => `${l.count} ${l.folder.toLowerCase()}`).join(" · ")} · ${gbText(v.free)} free`;
    $("md-space-bar").style.width = `${100 * (v.total - v.free) / v.total}%`;
    $("md-space").textContent = `${gbText(v.free)} free of ${gbText(v.total)}`;
    $("md-libraries").replaceChildren(...v.libraries.map((l) => {
      const row = make("div", "row");
      const text = make("span", "r-text");
      text.append(make("span", "r-name", `${l.folder} · ${l.count}`), make("span", "r-sub", l.recent.length ? `Newest: ${l.recent.slice(0, 4).join(", ")}` : "Empty so far"));
      row.append(text);
      return row;
    }));
  } catch (e) {
    setDot("md-dot", "unknown");
    $("md-state").textContent = e.message;
    if (!$("md-libraries").querySelector(".row")) $("md-libraries").replaceChildren(make("p", "empty-note", e.message));
  }
}

let jfInfo = null;

function jfTile(item) {
  const tile = make("button", "jf-tile");
  tile.type = "button";
  if (item.poster) {
    const img = make("img");
    img.src = item.poster;
    img.alt = "";
    img.loading = "lazy";
    tile.append(img);
  } else tile.append(make("span", "jf-noposter", (item.title || "?").slice(0, 1)));
  if (item.progress) {
    const bar = make("span", "jf-progress");
    const fill = make("span");
    fill.style.width = `${item.progress}%`;
    bar.append(fill);
    tile.append(bar);
  } else if (item.watched) tile.append(make("span", "jf-watched", "Watched"));
  tile.append(make("span", "jf-name", item.title), make("span", "jf-sub", item.detail || item.kind));
  tile.addEventListener("click", () => jfOpen(item.id));
  return tile;
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

async function refreshJellyfin() {
  const on = (features.devices || []).includes("jellyfin");
  $("jf-browse").hidden = !on;
  if (!on) return;
  try {
    const r = await fetch("/api/dev/jellyfin", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    jfInfo = v;
    $("md-open").href = v.web;
    const c = v.counts;
    $("jf-counts").textContent = [plural(c.movies, "movie"), plural(c.shows, "show"), c.songs ? plural(c.songs, "song") : ""].filter(Boolean).join(" · ");
    $("jf-resume").replaceChildren(...v.resume.map(jfTile));
    $("jf-resume-card").hidden = !v.resume.length;
    $("jf-latest").replaceChildren(...v.latest.map(jfTile));
    $("jf-latest-empty").hidden = v.latest.length > 0;
  } catch (e) {
    $("jf-counts").textContent = e.message;
  }
}

async function jfOpen(id) {
  try {
    const d = await devPost("jellyfin", { action: "details", id });
    $("jf-d-poster").hidden = !d.poster;
    if (d.poster) $("jf-d-poster").src = d.poster;
    $("jf-d-initial").hidden = !!d.poster;
    $("jf-d-initial").textContent = (d.title || "?").slice(0, 1);
    $("jf-d-title").textContent = d.title;
    $("jf-d-meta").textContent = [d.detail && d.kind !== "movie" ? d.detail : d.year, d.minutes ? `${d.minutes} min` : "", d.rating, d.score ? `★ ${d.score.toFixed(1)}` : "", (d.genres || []).join(", ")].filter(Boolean).join(" · ");
    $("jf-d-next").hidden = !d.next;
    $("jf-d-next").textContent = d.next ? `Next up: ${d.next.label}` : "";
    $("jf-d-overview").textContent = d.overview || "";
    const web = jfInfo ? jfInfo.web : $("md-open").href;
    $("jf-d-open").href = `${web}/web/#/details?id=${encodeURIComponent(d.id)}`;
    const targets = jfInfo ? jfInfo.targets : [];
    $("jf-d-play").replaceChildren(...targets.flatMap((t) => {
      const play = make("button", "primary", `Play on ${t.name}`);
      play.type = "button";
      play.addEventListener("click", async () => {
        play.disabled = true;
        try { toast((await devPost("jellyfin", { action: "play", id: d.id, target: t.id })).text); } catch (e) { toast(e.message); }
        play.disabled = false;
      });
      const stop = make("button", "", "Stop");
      stop.type = "button";
      stop.setAttribute("aria-label", `Stop on ${t.name}`);
      stop.addEventListener("click", async () => {
        try { toast((await devPost("jellyfin", { action: "stop", target: t.id })).text); } catch (e) { toast(e.message); }
      });
      return [play, stop];
    }));
    $("jf-detail").hidden = false;
    $("jf-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    toast(e.message);
  }
}

$("jf-d-close").addEventListener("click", () => { $("jf-detail").hidden = true; });

$("jf-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("jf-q").value.trim();
  if (!query) { $("jf-results-card").hidden = true; return; }
  try {
    const v = await devPost("jellyfin", { action: "search", query });
    $("jf-results").replaceChildren(...(v.results.length ? v.results.map(jfTile) : [make("p", "muted", `Nothing called “${query}” yet.`)]));
    $("jf-results-card").hidden = false;
  } catch (err) {
    toast(err.message);
  }
});

let phNext = null;

async function devPost(name, body) {
  const r = await fetch(`/api/dev/${name}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const v = await r.json();
  if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
  return v;
}

function whenText(iso) {
  if (!iso) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const [y, m, day] = iso.split("-").map(Number);
    return new Date(y, m - 1, day).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  }
  const d = new Date(iso);
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days === 0) return `today ${time}`;
  if (days === 1) return `yesterday ${time}`;
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: days > 300 ? "numeric" : undefined });
}

function phTile(item) {
  const tile = make("button", "ph-tile");
  tile.type = "button";
  tile.setAttribute("aria-label", `${item.video ? "Video" : "Photo"} from ${whenText(item.taken)}`);
  if (item.thumb) {
    const img = make("img");
    img.src = item.thumb;
    img.alt = "";
    img.loading = "lazy";
    tile.append(img);
  }
  if (item.video) tile.append(make("span", "ph-badge", item.duration || "Video"));
  tile.addEventListener("click", () => phOpen(item));
  return tile;
}

async function refreshPhotos() {
  try {
    const r = await fetch("/api/dev/photos", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    setDot("ph-dot", "on");
    $("ph-state").textContent = `${v.photos.toLocaleString()} photos · ${v.videos.toLocaleString()} videos · ${gbText(v.usage)}`;
    $("sub-photos").textContent = `${v.photos + v.videos} backed up · newest ${whenText(v.last_upload)}`;
    $("ph-backup").textContent = v.last_upload ? `Last backup from your phone: ${whenText(v.last_upload)}` : "Nothing backed up yet.";
    if (v.disk.size) {
      $("ph-space-bar").style.width = `${100 * v.disk.used / v.disk.size}%`;
      $("ph-space").textContent = `${gbText(v.disk.free)} free of ${gbText(v.disk.size)} on the hub`;
    }
    $("ph-open").href = v.web;
    $("ph-grid").replaceChildren(...v.recent.map(phTile));
    phNext = v.next;
    $("ph-more").hidden = !phNext;
  } catch (e) {
    setDot("ph-dot", "unknown");
    $("ph-state").textContent = e.message;
  }
}

async function phOpen(item) {
  $("ph-v-when").textContent = `${item.video ? "Video" : "Photo"} · ${whenText(item.taken)}`;
  $("ph-v-img").removeAttribute("src");
  $("ph-viewer").hidden = false;
  $("ph-viewer").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const v = await devPost("photos", { action: "photo", id: item.id });
    $("ph-v-img").src = v.image;
    $("ph-v-open").href = v.open;
  } catch (e) {
    toast(e.message);
  }
}

$("ph-v-close").addEventListener("click", () => { $("ph-viewer").hidden = true; });
$("ph-refresh").addEventListener("click", refreshPhotos);
$("ph-more").addEventListener("click", async () => {
  if (!phNext) return;
  $("ph-more").disabled = true;
  try {
    const v = await devPost("photos", { action: "more", page: Number(phNext) });
    $("ph-grid").append(...v.items.map(phTile));
    phNext = v.next;
    $("ph-more").hidden = !phNext;
  } catch (e) {
    toast(e.message);
  }
  $("ph-more").disabled = false;
});

function dcRow(doc) {
  const row = make("a", "row");
  row.href = doc.open;
  row.target = "_blank";
  row.rel = "noopener";
  if (doc.thumb) {
    const img = make("img", "dc-thumb");
    img.src = doc.thumb;
    img.alt = "";
    img.loading = "lazy";
    row.append(img);
  } else row.append(make("span", "dc-thumb"));
  const text = make("span", "r-text");
  text.append(make("span", "r-name", doc.title), make("span", "r-sub", [doc.from, doc.created ? `dated ${whenText(String(doc.created).slice(0, 10))}` : "", doc.added ? `added ${whenText(doc.added)}` : "", doc.pages ? plural(doc.pages, "page") : ""].filter(Boolean).join(" · ")));
  if (doc.hit) text.append(make("span", "dc-hit", doc.hit.replace(/<[^>]+>/g, "")));
  row.append(text);
  return row;
}

async function refreshDocs() {
  try {
    const r = await fetch("/api/dev/docs", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    setDot("dc-dot", "on");
    $("dc-state").textContent = `${plural(v.total, "document")}${v.working ? ` · reading ${v.working} now` : ""}`;
    $("sub-docs").textContent = plural(v.total, "document");
    $("dc-open").href = v.web;
    $("dc-recent").replaceChildren(...v.recent.map(dcRow));
    $("dc-empty").hidden = v.recent.length > 0;
  } catch (e) {
    setDot("dc-dot", "unknown");
    $("dc-state").textContent = e.message;
  }
}

async function dcUpload(files) {
  for (const file of files) {
    $("dc-upload").textContent = `Sending ${file.name}…`;
    try {
      const r = await fetch(`/api/docs/upload?name=${encodeURIComponent(file.name)}`, { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file });
      const v = await r.json();
      if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
      $("dc-upload").textContent = v.text;
      if (v.task) dcWatch(v.task, file.name);
    } catch (e) {
      $("dc-upload").textContent = `${file.name}: ${e.message}`;
    }
  }
}

async function dcWatch(task, name) {
  for (let i = 0; i < 60; i++) {
    await new Promise((done) => setTimeout(done, 3000));
    try {
      const v = await devPost("docs", { action: "task", id: task });
      if (v.status === "SUCCESS") { $("dc-upload").textContent = `${name} is in Paperless.`; refreshDocs(); return; }
      if (v.status === "FAILURE") { $("dc-upload").textContent = `Paperless couldn't read ${name}: ${v.message}`; return; }
    } catch (e) {
      $("dc-upload").textContent = `Sent ${name}, but couldn't check on it: ${e.message}`;
      return;
    }
  }
}

$("dc-scan").addEventListener("change", (e) => { dcUpload([...e.target.files]); e.target.value = ""; });
$("dc-files").addEventListener("change", (e) => { dcUpload([...e.target.files]); e.target.value = ""; });
$("dc-refresh").addEventListener("click", refreshDocs);
$("dc-search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("dc-q").value.trim();
  if (!query) { $("dc-results-card").hidden = true; return; }
  try {
    const v = await devPost("docs", { action: "search", query });
    $("dc-results").replaceChildren(...(v.results.length ? v.results.map(dcRow) : [make("p", "muted", `No documents mention “${query}”.`)]));
    $("dc-results-card").hidden = false;
  } catch (err) {
    toast(err.message);
  }
});

function guessMovie(name) {
  const stem = name.replace(/\.[^.]+$/, "").replace(/[._]+/g, " ");
  const m = /^(.*?)[\s(\[]*((?:19|20)\d{2})(?:[\s)\]]|$)/.exec(stem);
  return m ? { title: m[1].replace(/[\s\-–(]+$/, "").trim(), year: m[2] } : { title: stem.trim(), year: "" };
}

function setKind(kind) {
  mdKind = kind;
  document.querySelectorAll("#md-kind button").forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
  $("md-movie-fields").hidden = kind !== "movie";
  $("md-show-fields").hidden = kind !== "show";
  $("md-music-fields").hidden = kind !== "music";
  pickFiles(mdFiles);
}

function pickFiles(list) {
  const okType = mdKind === "music" ? MD_AUDIO : MD_VIDEO;
  const all = [...list];
  mdFiles = all.filter((f) => okType.test(f.name));
  const skipped = all.length - mdFiles.length;
  const size = mdFiles.reduce((n, f) => n + f.size, 0);
  $("md-picked").textContent = mdFiles.length ? `${mdFiles.length} file${mdFiles.length === 1 ? "" : "s"}, ${gbText(size)}${skipped ? ` (${skipped} skipped: not ${mdKind === "music" ? "audio" : "video"} files)` : ""}`
    : all.length ? `None of those are ${mdKind === "music" ? "audio" : "video"} files.` : "Movies and shows: video files (and subtitles). Music: audio files.";
  $("md-go").disabled = !mdFiles.length || mdBusy;
  if (mdKind === "movie" && mdFiles.length && !$("md-title").value) {
    const video = mdFiles.find((f) => !/\.(srt|ass|ssa|vtt|sub|idx|jpg|jpeg|png|nfo)$/i.test(f.name)) || mdFiles[0];
    const guess = guessMovie(video.name);
    $("md-title").placeholder = guess.title || "From the file name";
    $("md-year").placeholder = guess.year || "e.g. 2010";
  }
  if (mdKind === "show" && mdFiles.length && !$("md-show").value && mdFiles[0].webkitRelativePath) {
    const parts = mdFiles[0].webkitRelativePath.split("/");
    $("md-show").placeholder = parts[0].replace(/[._]+/g, " ").replace(/\s*season\s*\d+.*$/i, "").trim() || "e.g. The Office";
  }
}

function queueRow(file) {
  const li = make("li", "md-item");
  const text = make("span", "grow");
  text.append(make("strong", null, file.name), make("small", null, gbText(file.size)));
  const state = make("span", "s-note", "Waiting");
  const bar = make("div", "mini-bar");
  const fill = make("span");
  bar.append(fill);
  li.append(text, state, bar);
  $("md-queue").append(li);
  return { state, fill, small: text.querySelector("small") };
}

async function uploadOne(file, details, ui) {
  const started = await devApi("media", { action: "start", kind: mdKind, name: file.name, size: file.size, ...details });
  if (!started.ok) throw new Error(started.text);
  if (started.done) { ui.fill.style.width = "100%"; return started.text; }
  ui.small.textContent = `→ ${started.path}`;
  let offset = started.received || 0;
  const chunk = 16 * 1024 * 1024;
  let failures = 0;
  const t0 = Date.now();
  while (offset < file.size) {
    try {
      const r = await fetch(`/api/media/upload/${encodeURIComponent(started.id)}?offset=${offset}`, {
        method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file.slice(offset, Math.min(file.size, offset + chunk)),
      });
      const out = await r.json().catch(() => ({ error: `Error ${r.status}` }));
      if (!r.ok) throw new Error(out.error || `Error ${r.status}`);
      offset = out.received;
      failures = 0;
      const pct = 100 * offset / file.size;
      const rate = offset / Math.max(1, (Date.now() - t0) / 1000);
      ui.fill.style.width = `${pct}%`;
      ui.state.textContent = `${Math.floor(pct)}% · ${(rate * 8 / 1e6).toFixed(0)} Mbit/s`;
    } catch (e) {
      failures += 1;
      if (failures > 6) throw e;
      ui.state.textContent = `Retrying… (${failures})`;
      await new Promise((ok) => setTimeout(ok, 2000 * failures));
    }
  }
  const done = await devApi("media", { action: "finish", id: started.id });
  if (!done.ok) throw new Error(done.text);
  return done.text;
}

document.querySelectorAll("#md-kind button").forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));
$("md-files").addEventListener("change", (e) => pickFiles(e.target.files));
$("md-folder").addEventListener("change", (e) => pickFiles(e.target.files));
$("md-refresh").addEventListener("click", refreshMedia);
$("md-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (mdBusy || !mdFiles.length) return;
  mdBusy = true;
  $("md-go").disabled = true;
  $("md-go").textContent = "Uploading…";
  $("md-queue-card").hidden = false;
  $("md-queue").replaceChildren();
  const files = [...mdFiles];
  const total = files.reduce((n, f) => n + f.size, 0);
  $("md-total").textContent = `${files.length} file${files.length === 1 ? "" : "s"} · ${gbText(total)}`;
  let good = 0;
  for (const file of files) {
    const ui = queueRow(file);
    let details = {};
    if (mdKind === "movie") {
      const guess = guessMovie(file.name);
      details = { title: $("md-title").value.trim() || guess.title, year: $("md-year").value.trim() || guess.year };
    } else if (mdKind === "show") {
      const folder = file.webkitRelativePath ? file.webkitRelativePath.split("/")[0].replace(/[._]+/g, " ").replace(/\s*season\s*\d+.*$/i, "").trim() : "";
      details = { title: $("md-show").value.trim() || folder, season: $("md-season").value.trim() };
    } else {
      details = { title: $("md-artist").value.trim(), album: $("md-album").value.trim() };
    }
    try {
      ui.state.textContent = "Starting…";
      const text = await uploadOne(file, details, ui);
      ui.state.textContent = "Done";
      ui.state.classList.add("md-ok");
      good += 1;
    } catch (err) {
      ui.state.textContent = err.message;
      ui.state.classList.add("md-bad");
    }
  }
  if (good) {
    const out = await devApi("media", { action: "refresh" });
    toast(`${good} of ${files.length} uploaded. ${out.text || ""}`);
  } else {
    toast("Nothing was uploaded.");
  }
  mdBusy = false;
  $("md-go").textContent = "Upload";
  $("md-files").value = "";
  $("md-folder").value = "";
  pickFiles([]);
  ["md-title", "md-year", "md-show", "md-season", "md-artist", "md-album"].forEach((id) => { $(id).value = ""; });
  refreshMedia();
});
window.addEventListener("beforeunload", (e) => { if (mdBusy) { e.preventDefault(); e.returnValue = ""; } });

async function refreshApps() {
  try {
    const r = await fetch("/api/apps", { cache: "no-store" });
    if (!r.ok) throw new Error(`The hub answered with error ${r.status}.`);
    const { apps } = await r.json();
    if (!apps.length) {
      $("app-list").replaceChildren(make("p", "card muted", "No apps added yet."));
      return;
    }
    const card = document.createElement("div");
    card.className = "card list-card";
    card.append(...apps.map((a) => {
      const link = document.createElement("a");
      link.className = "row app-row";
      link.href = a.url;
      link.target = "_blank";
      link.rel = "noopener";
      const text = document.createElement("span");
      text.className = "r-text";
      const name = document.createElement("span");
      name.className = "r-name";
      name.textContent = a.name;
      const sub = document.createElement("span");
      sub.className = "r-sub";
      sub.textContent = a.up ? "Running" : "Not answering";
      text.append(name, sub);
      const dot = document.createElement("span");
      dot.className = `dot ${a.up ? "on" : "off"}`;
      const chev = icon("chevron");
      chev.classList.add("chev");
      link.append(appIcon(a.name), text, dot, chev);
      return link;
    }));
    $("app-list").replaceChildren(card);
  } catch (e) {
    const text = failText(e).replace(/^The/, "the").replace(/\.$/, "");
    if ($("app-list").querySelector(".app-row")) toast(`Couldn't update the apps: ${text}`);
    else $("app-list").replaceChildren(make("p", "card muted", `Couldn't check the apps: ${text}`));
  }
}

function localInput(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function wakeAt() {
  if (powerAction !== "off" || !$("power-wake-on").checked) return null;
  const when = new Date($("power-wake-time").value);
  return Number.isNaN(when.getTime()) ? NaN : Math.round(when.getTime() / 1000);
}

function powerQuestion() {
  if (powerAction === "off") {
    const at = wakeAt();
    $("power-question").textContent = at
      ? `Shut down the mini PC? Everything on it goes off: this app, the VPN, the assistant, Minecraft, Immich and Paperless. It turns itself back on ${new Date(at * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}.`
      : "Shut down the mini PC? Everything on it goes off: this app, the VPN, the assistant, Minecraft, Immich and Paperless. Only its power button turns it back on, so from away it stays off until someone's home.";
    $("power-yes").textContent = "Yes, shut down";
  } else {
    $("power-question").textContent = powerAction === "hub"
      ? "Restart the whole mini PC? The assistant and VPN are offline for about a minute, then start again by themselves."
      : "Restart the assistant? It's unavailable for about half a minute.";
    $("power-yes").textContent = "Yes, restart";
  }
  $("power-wake-label").hidden = !$("power-wake-on").checked;
}

document.querySelectorAll("[data-power]").forEach((b) => b.addEventListener("click", () => {
  powerAction = b.dataset.power;
  $("power-wake-box").hidden = powerAction !== "off";
  if (powerAction === "off") {
    const morning = new Date();
    morning.setDate(morning.getDate() + (morning.getHours() >= 7 ? 1 : 0));
    morning.setHours(7, 0, 0, 0);
    $("power-wake-time").value = localInput(morning);
  }
  powerQuestion();
  $("power-confirm").hidden = false;
}));
$("power-wake-on").addEventListener("change", powerQuestion);
$("power-wake-time").addEventListener("input", powerQuestion);
$("power-no").addEventListener("click", () => { $("power-confirm").hidden = true; powerAction = null; });
$("power-yes").addEventListener("click", async () => {
  if (!powerAction) return;
  const wake = wakeAt();
  if (Number.isNaN(wake)) {
    toast("Pick the time it should turn back on.");
    return;
  }
  $("power-confirm").hidden = true;
  let before = {};
  try { before = await (await fetch("/api/system", { cache: "no-store" })).json(); } catch (e) {}
  let r;
  try {
    r = await fetch("/api/power", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(wake ? { action: powerAction, wake_at: wake } : { action: powerAction }) });
  } catch (e) {
    showRestart("The hub isn't answering", "Nothing was sent. Check the connection and try again.");
    powerAction = null;
    return;
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showRestart(powerAction === "off" ? "Couldn't shut down" : "Couldn't restart", body.message || `Error ${r.status}`);
  } else {
    restart = { action: powerAction, wake, at: Date.now(), booted: before.booted_at || 0, started: (before.assistant || {}).started_at || 0 };
    showRestart(`${body.message || "Shutting down"}.`, "Shutting down…");
  }
  powerAction = null;
});

$("add-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const list = selectedList;
  const item = $("add-text").value.trim();
  if (!item) return;
  $("add-text").value = "";
  const out = await control("list.add", { list, item });
  toast(out.ok ? `Added ${item}.` : out.text);
  refresh();
});

async function press(button, action, body, quiet) {
  button.disabled = true;
  const out = await control(action, body);
  button.disabled = false;
  if (!out.ok || !quiet) toast(out.text || (out.ok ? "Done." : "That didn't work."));
  return out;
}

document.querySelectorAll("[data-key]").forEach((b) => b.addEventListener("click", () => press(b, "tv.key", { key: b.dataset.key }, true)));
let tvView = null;
let tvIcons = {};
let tvTimer = null;
let tvBusy = false;
const APP_BRANDS = { "Netflix": ["netflix", "c-netflix"], "YouTube": ["youtube", "c-youtube"], "YouTube Music": ["youtube", "c-youtube"],
  "YouTube TV": ["youtube", "c-youtube"], "Prime Video": ["primevideo", "c-primevideo"], "Spotify": ["spotify", "c-spotify"],
  "Paramount+": ["paramountplus", "c-paramountplus"], "Jellyfin": ["jellyfin", "c-jellyfin"], "PS Remote Play": ["playstation", "c-playstation"],
  "Max": ["max", "c-max"], "HBO Max": ["max", "c-max"] };
const APP_MONO = { "Hulu": ["h", "mono-hulu"], "Disney+": ["D", "mono-disney"], "Peacock TV": ["P", "mono-peacock"], "Crunchyroll": ["C", "mono-crunchy"],
  "Tubi": ["T", "mono-tubi"], "Play Store": ["▶", "mono-play"] };

function appBadge(title) {
  if (APP_BRANDS[title]) {
    const svg = icon("x");
    svg.classList.add("logo", APP_BRANDS[title][1]);
    svg.firstChild.setAttribute("href", `#b-${APP_BRANDS[title][0]}`);
    return svg;
  }
  const mono = document.createElement("span");
  const [letter, cls] = APP_MONO[title] || [title[0], "mono-other"];
  mono.className = `mono ${cls}`;
  mono.textContent = letter;
  return mono;
}

async function refreshTv() {
  if (tvBusy) return;
  tvBusy = true;
  try {
    const r = await fetch("/api/dev/tv", { cache: "no-store" });
    const v = await r.json();
    if (!r.ok) throw new Error(v.error || `Error ${r.status}`);
    tvView = v;
    renderTv(v);
    if (v.on && !Object.keys(tvIcons).length) {
      const out = await devApi("tv", { action: "icons" });
      if (out.ok && Object.keys(out.icons).length) { tvIcons = out.icons; $("tv-apps").dataset.key = ""; renderTv(tvView); }
    }
  } catch (e) {
    $("tv-state").textContent = e.message || "Not answering";
    setDot("tv-dot", "unknown");
  } finally {
    tvBusy = false;
  }
}

function renderTv(v) {
  setDot("tv-dot", v.on ? "on" : "off");
  $("tv-state").textContent = v.on ? `On · ${v.showing ? v.showing.title : "apps or home screen"}${v.screen_off ? " · screen off" : ""}` : "Off";
  document.querySelectorAll("[data-tv-power]").forEach((b) => {
    b.hidden = (b.dataset.tvPower === "1") === v.on;
    b.classList.toggle("primary", b.dataset.tvPower === "1");
  });
  $("tv-live").hidden = !v.on;
  if (!v.on) return;
  $("tv-inputs").replaceChildren(...v.inputs.map((i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = i.active ? "active" : "";
    const name = document.createElement("strong");
    name.textContent = i.title.replace(" (eARC/ARC)", "");
    const small = document.createElement("small");
    const dot = document.createElement("span");
    dot.className = `dot ${i.connected ? "on" : "off"}`;
    small.append(dot, document.createTextNode(i.device || i.label || (i.title.includes("eARC") ? "Soundbar (eARC)" : i.connected ? "Connected" : "Nothing plugged in")));
    b.append(name, small);
    b.addEventListener("click", async () => {
      b.disabled = true;
      const out = await devApi("tv", { action: "input", uri: i.uri });
      b.disabled = false;
      toast(out.text);
      setTimeout(refreshTv, 1200);
    });
    return b;
  }));
  const key = JSON.stringify(v.apps.map((a) => a.uri));
  if ($("tv-apps").dataset.key !== key) {
    $("tv-apps").dataset.key = key;
    $("tv-apps").replaceChildren(...v.apps.map((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "app-chip";
      if (tvIcons[a.uri]) {
        const img = iconImage(tvIcons[a.uri]);
        img.className = "app-icon";
        b.append(img, document.createTextNode(a.title));
      } else {
        b.append(appBadge(a.title), document.createTextNode(a.title));
      }
      b.addEventListener("click", async () => {
        b.disabled = true;
        const out = await devApi("tv", { action: "app", uri: a.uri });
        b.disabled = false;
        toast(out.text);
        setTimeout(refreshTv, 2000);
      });
      return b;
    }));
  }
  $("tv-settings").hidden = !v.picture;
  if (!v.picture) {
    if (v.settling) setTimeout(refreshTv, 2000);
    return;
  }
  const pic = v.picture;
  $("tv-mode").replaceChildren(...pic.modes.map((m) => Object.assign(document.createElement("option"), { value: m.value, textContent: m.name, selected: m.value === pic.mode })));
  if (!pic.modes.some((m) => m.value === pic.mode)) $("tv-mode").prepend(Object.assign(document.createElement("option"), { value: "", textContent: pic.mode, selected: true }));
  if (document.activeElement !== $("tv-bright")) {
    $("tv-bright").min = pic.brightness_min;
    $("tv-bright").max = pic.brightness_max;
    $("tv-bright").value = pic.brightness ?? 0;
    $("tv-bright-label").textContent = pic.brightness ?? "–";
  }
  $("tv-bright").disabled = pic.brightness == null;
  $("tv-light").checked = pic.light_sensor === "on";
  $("tv-light").disabled = pic.light_sensor == null;
  document.querySelectorAll("[data-out]").forEach((b) => b.classList.toggle("active", b.dataset.out === v.output));
  $("tv-screen").checked = !!v.screen_off;
}

async function tvSet(body, quiet) {
  const out = await devApi("tv", body);
  if (!out.ok || !quiet) toast(out.text);
  refreshTv();
  return out;
}

document.querySelectorAll("[data-tv-power]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  $("tv-state").textContent = b.dataset.tvPower === "1" ? "Turning on…" : "Turning off…";
  await tvSet({ action: "power", on: b.dataset.tvPower === "1" });
  b.disabled = false;
}));
$("tv-refresh").addEventListener("click", refreshTv);
$("tv-close").addEventListener("click", () => tvSet({ action: "close_apps" }));
$("tv-mode").addEventListener("change", (e) => tvSet({ action: "picture_mode", value: e.target.value }));
$("tv-bright").addEventListener("input", (e) => { $("tv-bright-label").textContent = e.target.value; });
$("tv-bright").addEventListener("change", (e) => tvSet({ action: "brightness", value: Number(e.target.value) }, true));
$("tv-light").addEventListener("change", (e) => tvSet({ action: "light_sensor", on: e.target.checked }));
$("tv-screen").addEventListener("change", (e) => tvSet({ action: "screen", off: e.target.checked }));
document.querySelectorAll("[data-out]").forEach((b) => b.addEventListener("click", () => tvSet({ action: "output", value: b.dataset.out })));
document.querySelectorAll("[data-music]").forEach((b) => b.addEventListener("click", async () => {
  await press(b, "music.control", { do: b.dataset.music }, true);
  setTimeout(refreshMusic, 800);
}));
$("music-refresh").addEventListener("click", refreshMusic);
document.querySelectorAll("[data-roku-key]").forEach((b) => b.addEventListener("click", () => press(b, "roku.key", { key: b.dataset.rokuKey }, true)));
$("roku-refresh").addEventListener("click", refreshRoku);
$("ps5-refresh").addEventListener("click", refreshPs5);
$("px-refresh").addEventListener("click", refreshProxmox);

async function powerPress(button, action, on, refresher, waitText) {
  const out = await press(button, action, { on });
  if (out.ok) {
    $("ps5-state").textContent = waitText(on);
    setTimeout(refresher, on ? 20000 : 8000);
  }
}

document.querySelectorAll("[data-ps5-power]").forEach((b) => b.addEventListener("click", () =>
  powerPress(b, "ps5.power", b.dataset.ps5Power === "1", refreshPs5, (on) => on ? "Starting up…" : "Going into rest mode; the TV turns off too…")));

async function serverPower(button, action) {
  button.disabled = true;
  const out = await devApi("proxmox", { action });
  button.disabled = false;
  toast(out.text);
  if (out.ok) {
    $("px-state").textContent = action === "on" ? "Starting up…" : action === "reboot" ? "Restarting…" : "Shutting down…";
    setDot("px-dot", "rest");
  }
}

document.querySelectorAll("[data-px-power]").forEach((b) => b.addEventListener("click", () => {
  const action = b.dataset.pxPower;
  if (action === "on") return serverPower(b, action);
  pxPending = { kind: "power", action, button: b };
  $("px-question").textContent = action === "reboot"
    ? "Restart the Proxmox server? Everything running on it stops cleanly and starts again; it's back in about a minute."
    : "Shut down the Proxmox server? Everything running on it stops cleanly first. You can turn it back on from here.";
  $("px-yes").textContent = action === "reboot" ? "Yes, restart" : "Yes, shut down";
  $("px-confirm").hidden = false;
}));
$("px-no").addEventListener("click", () => { $("px-confirm").hidden = true; pxPending = null; });
$("px-yes").addEventListener("click", () => {
  $("px-confirm").hidden = true;
  const job = pxPending;
  pxPending = null;
  if (!job) return;
  if (job.kind === "power") serverPower(job.button, job.action);
  else if (job.kind === "install") installUpdates();
  else guestAction(job.button, job.guest, job.action);
});

async function updateWatch() {
  const st = await devApi("proxmox", { action: "update_state" });
  if (!st.ok) return;
  $("px-update-state").hidden = !st.running && !st.reboot_needed;
  $("px-update-state").textContent = st.running ? "Installing…" : st.reboot_needed ? "Restart needed" : "";
  $("px-update-state").className = `state ${st.running ? "starting" : st.reboot_needed ? "problem" : ""}`;
  $("px-update-note").textContent = st.running ? "Installing now. Anything running keeps running."
    : st.reboot_needed ? "A new Linux kernel was installed. Restart the server when it suits you to start using it."
    : st.result != null && st.result !== 0 ? "The last install didn't finish cleanly. Try again, or ask Claude to take a look."
    : "Proxmox and Debian security and bug-fix updates from the free update channel.";
  if (st.running) setTimeout(() => { if (current === "proxmox") { updateWatch(); refreshProxmox(); } }, 8000);
}

async function installUpdates() {
  $("px-install").disabled = true;
  const out = await devApi("proxmox", { action: "install_updates" });
  $("px-install").disabled = false;
  toast(out.text);
  updateWatch();
}

$("px-check").addEventListener("click", async () => {
  $("px-check").disabled = true;
  $("px-updates").textContent = "Checking…";
  const out = await devApi("proxmox", { action: "check_updates" });
  $("px-check").disabled = false;
  toast(out.text);
  refreshProxmox();
});
$("px-install").addEventListener("click", () => {
  pxPending = { kind: "install" };
  $("px-question").textContent = `Install ${pxView && pxView.updates ? pxView.updates : "the waiting"} update${pxView && pxView.updates === 1 ? "" : "s"}? It takes a few minutes and anything running keeps running. Some updates need a restart afterwards; the page will say so.`;
  $("px-yes").textContent = "Yes, install";
  $("px-confirm").hidden = false;
});
$("music-volume").addEventListener("change", (e) => press(e.target, "music.control", { do: "volume", value: Number(e.target.value) }));

$("yes").addEventListener("click", () => answer(true));
$("no").addEventListener("click", () => answer(false));
window.addEventListener("hashchange", route);
$("back").addEventListener("click", () => { location.hash = PARENT[current] || "main"; });

function loadFeatures() {
  return fetch("/api/features", { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`Error ${r.status}`);
    return r.json();
  }).then(applyFeatures);
}

function applyFeatures(f) {
  features = { ...f, known: true };
  const devs = f.devices || [];
  $("bubble-photos").hidden = !devs.includes("photos");
  $("sub-photos").textContent = "Your phone's photo backup";
  $("bubble-docs").hidden = !devs.includes("docs");
  $("sub-docs").textContent = "Search, scan and upload paperwork";
  $("bubble-media").hidden = !["media", "jellyfin"].some((d) => (f.devices || []).includes(d));
  if (/Android|iPhone|iPad/i.test(navigator.userAgent)) $("md-folder-label").hidden = true;
  $("bubble-mc").hidden = !f.minecraft;
  $("bubble-router").hidden = !f.router;
  $("bubble-packages").hidden = !f.packages;
}

featuresLoading = true;
loadFeatures().catch(() => {}).finally(() => { featuresLoading = false; route(); });
let stateTimer = setInterval(refresh, 1500);
const RELOAD_LOSES_WORK = ["mcnew", "photos", "docs", "media"];

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    [stateTimer, systemTimer, adTimer, rokuTimer, tvTimer, psnTimer, pxTimer].forEach(clearInterval);
    stateTimer = systemTimer = adTimer = rokuTimer = tvTimer = psnTimer = pxTimer = null;
    viewHooks.forEach((hook) => hook(null, []));
    return;
  }
  if (stateTimer) return;
  stateTimer = setInterval(refresh, 1500);
  if (RELOAD_LOSES_WORK.includes(current) || (current === "mcserver" && !["overview", "console"].includes(mcTab))) {
    refresh();
    return;
  }
  const main = document.querySelector("main");
  const top = main.scrollTop;
  route();
  main.scrollTop = top;
});
