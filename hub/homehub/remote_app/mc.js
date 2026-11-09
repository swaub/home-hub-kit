const LOADER_HELP = {
  paper: "Fast, and runs plugins. The usual choice for playing with friends without mods.",
  fabric: "Lightweight mods. Players need Fabric and the same mods installed.",
  forge: "The classic mod loader with the biggest modpacks. Players need Forge and the same mods.",
  neoforge: "The newer fork of Forge most new mods use. Players need NeoForge and the same mods.",
  vanilla: "Plain Minecraft, no mods or plugins.",
};
const STATE_TEXT = { online: "Online", starting: "Starting", stopped: "Stopped", installing: "Installing", problem: "Problem" };
let mcTimer = null;
let mcSlug = "";
let mcTab = "overview";
let mcInfo = null;
let mcPath = "";
let mcEditing = "";
let mcCmdKind = "chat";
let mcLogKey = "";
let mcAnswer = null;

function mcAsk(anchor, question, yes, danger) {
  if (mcAnswer) mcAnswer(false);
  const box = $("mc-confirm");
  anchor.after(box);
  $("mc-question").textContent = question;
  $("mc-yes").textContent = yes;
  $("mc-yes").className = danger ? "danger-button" : "primary";
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return new Promise((done) => {
    mcAnswer = (ok) => {
      mcAnswer = null;
      box.hidden = true;
      done(ok);
    };
  });
}

$("mc-yes").addEventListener("click", () => { if (mcAnswer) mcAnswer(true); });
$("mc-no").addEventListener("click", () => { if (mcAnswer) mcAnswer(false); });

function fileIcon(name) {
  const svg = icon(name);
  svg.classList.add("file-icon");
  return svg;
}

function trashButton(label) {
  const b = make("button", "ghost icon-btn");
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.append(icon("trash"));
  return b;
}

async function api(path, options = {}) {
  const r = await fetch(path, { cache: "no-store", ...options });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `Error ${r.status}`);
  return body;
}

const postJson = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const enc = encodeURIComponent;
const sizeText = (n) => (n == null ? "–" : bytes(n));

function stateBadge(state) {
  const b = make("span", `state ${state}`, STATE_TEXT[state] || state);
  return b;
}

async function refreshMcList() {
  try {
    const { servers } = await api("/api/mc");
    const online = servers.filter((s) => s.state === "online").length;
    $("sub-mc").textContent = servers.length ? `${servers.length} server${servers.length > 1 ? "s" : ""} · ${online} online` : "Create your first server";
    if (current !== "mc") return;
    if (!servers.length) {
      $("mc-list").replaceChildren(make("p", "card muted", "No servers yet. Tap New server to make one."));
      return;
    }
    $("mc-list").replaceChildren(...servers.map((s) => {
      const a = make("a", "card server-row");
      a.href = `#mc/${enc(s.slug)}`;
      const head = make("div", "card-head");
      head.append(make("h2", null, s.name), stateBadge(s.state));
      a.append(head, make("p", "muted", `${s.loader[0].toUpperCase()}${s.loader.slice(1)} ${s.version} · ${s.memory_gb} GB · up to ${s.max_players} players${s.public ? " · open to friends" : ""}`));
      if (s.note) a.append(make("p", "notice-line", s.note));
      return a;
    }));
  } catch (e) {
    $("sub-mc").textContent = "Not answering";
    if (current === "mc") $("mc-list").replaceChildren(make("p", "card muted", e.message));
  }
}

let formLoader = "paper";
let formPicked = new Map();
let formOffset = 0;
let searchTimer = null;

async function loadVersions() {
  $("mcf-version").replaceChildren(make("option", null, "Loading…"));
  $("mcf-loader-help").textContent = LOADER_HELP[formLoader];
  $("mcf-mods-box").hidden = formLoader === "vanilla";
  $("mcf-mods-title").textContent = formLoader === "paper" ? "Plugins" : "Mods";
  try {
    const { versions } = await api(`/api/mc/versions?loader=${formLoader}`);
    $("mcf-version").replaceChildren(...versions.map((v) => make("option", null, v)));
    searchMods("mcf", true);
  } catch (e) {
    $("mcf-version").replaceChildren(make("option", null, "Couldn't load versions"));
  }
}

function renderPicked(prefix, picked, onChange) {
  $(`${prefix}-picked`).replaceChildren(...[...picked.entries()].map(([slug, title]) => {
    const c = make("button", "chip picked", title);
    c.type = "button";
    c.setAttribute("aria-label", `Remove ${title}`);
    c.append(icon("x"));
    c.addEventListener("click", () => { picked.delete(slug); renderPicked(prefix, picked, onChange); onChange(); });
    return c;
  }));
}

async function searchMods(prefix, reset) {
  const loader = prefix === "mcf" ? formLoader : (mcInfo || {}).loader;
  const version = prefix === "mcf" ? $("mcf-version").value : (mcInfo || {}).version;
  const picked = prefix === "mcf" ? formPicked : serverPicked;
  if (!loader || loader === "vanilla" || !version) return;
  if (reset) formOffset = 0;
  try {
    const found = await api(`/api/mc/mods?loader=${loader}&version=${enc(version)}&q=${enc($(`${prefix}-search`).value)}&offset=${formOffset}`);
    const rows = found.hits.map((h) => {
      const li = make("li");
      const text = make("span", "grow", h.title);
      text.append(make("small", null, `${h.description} · ${h.downloads.toLocaleString()} downloads${h.client === "required" ? " · players need it too" : ""}`));
      const add = make("button", picked.has(h.slug) ? "ghost" : "", picked.has(h.slug) ? "Added" : "Add");
      add.type = "button";
      add.addEventListener("click", () => {
        picked.set(h.slug, h.title);
        add.textContent = "Added";
        add.className = "ghost";
        renderPicked(prefix, picked, () => prefix === "mcs" && markModsDirty());
        if (prefix === "mcs") markModsDirty();
      });
      li.append(text, add);
      return li;
    });
    if (reset) $(`${prefix}-results`).replaceChildren(...rows);
    else $(`${prefix}-results`).append(...rows);
    if (prefix === "mcf") $("mcf-more").hidden = formOffset + found.hits.length >= found.total;
  } catch (e) {
    $(`${prefix}-results`).replaceChildren(make("li", "empty-row", e.message));
  }
}

document.querySelectorAll("#mcf-loader button").forEach((b) => b.addEventListener("click", () => {
  formLoader = b.dataset.loader;
  document.querySelectorAll("#mcf-loader button").forEach((x) => x.classList.toggle("active", x === b));
  formPicked = new Map();
  renderPicked("mcf", formPicked, () => {});
  loadVersions();
}));
$("mcf-version").addEventListener("change", () => { formPicked = new Map(); renderPicked("mcf", formPicked, () => {}); searchMods("mcf", true); });
$("mcf-memory").addEventListener("input", () => { $("mcf-memory-label").textContent = `${$("mcf-memory").value} GB`; });
$("mcf-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => searchMods("mcf", true), 350); });
$("mcf-more").addEventListener("click", () => { formOffset += 20; searchMods("mcf", false); });
$("mcf-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const owner = $("mcf-owner").value.trim();
  const body = { name: $("mcf-name").value, loader: formLoader, version: $("mcf-version").value, memory_gb: Number($("mcf-memory").value),
    max_players: Number($("mcf-players").value), disk_gb: Number($("mcf-disk").value), difficulty: $("mcf-difficulty").value,
    mode: $("mcf-mode").value, mods: [...formPicked.keys()], players: owner ? [owner] : [], eula: $("mcf-eula").checked };
  $("mcf-create").disabled = true;
  try {
    const made = await postJson("/api/mc", body);
    toast("Creating the server. The first start downloads everything and takes a few minutes.");
    location.hash = `mc/${enc(made.slug)}/console`;
  } catch (err) {
    toast(err.message);
  } finally {
    $("mcf-create").disabled = false;
  }
});

let serverPicked = new Map();
let serverModsKey = "";

function markModsDirty() {
  $("mcs-mods-save").hidden = JSON.stringify([...serverPicked.keys()].sort()) === serverModsKey;
}

function showTab(tab) {
  if (mcAnswer) mcAnswer(false);
  mcTab = tab;
  document.querySelectorAll("#mcs-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".mcs-tab").forEach((t) => { t.hidden = t.id !== `mcs-${tab}`; });
  if (tab === "files") loadFiles(mcPath);
  if (tab === "mods") loadMods();
  if (tab === "settings" && mcInfo) fillSettings();
  if (tab === "console") { mcLogKey = ""; refreshLog(); }
}

document.querySelectorAll("#mcs-tabs button").forEach((b) => b.addEventListener("click", () => { location.hash = `mc/${enc(mcSlug)}/${b.dataset.tab}`; }));

function renderDetail(s) {
  mcInfo = s;
  $("title").textContent = s.name;
  $("mcs-name").textContent = s.name;
  $("mcs-state").replaceWith(Object.assign(stateBadge(s.state), { id: "mcs-state" }));
  $("mcs-kind").textContent = `${s.loader[0].toUpperCase()}${s.loader.slice(1)} ${s.version} · ${s.difficulty} · ${s.mode}`;
  $("mcs-note").hidden = !s.note;
  $("mcs-note").textContent = s.note;
  $("mcs-players").textContent = s.players_online == null ? "–" : `${s.players_online} / ${s.max_players}`;
  $("mcs-player-names").textContent = s.player_names.length ? s.player_names.join(", ") : s.state === "online" ? "Nobody on" : "";
  $("mcs-cpu").textContent = s.cpu_percent == null ? "–" : `${Math.round(s.cpu_percent / s.cpus)}%`;
  $("mcs-cpu-of").textContent = `of its ${s.cpus} threads`;
  $("mcs-tps").textContent = s.tps == null ? "–" : `${s.tps}`;
  $("mcs-mspt").textContent = s.mspt == null ? "" : s.mspt > 50 ? `Falling behind: ${s.mspt} ms per tick (needs under 50)` :
    `Keeping up: ${s.mspt} ms per tick of 50 · worst ${s.mspt_p95} ms`;
  $("mcs-mem").textContent = s.memory_used == null ? "–" : bytes(s.memory_used);
  $("mcs-mem-limit").textContent = `of ${s.memory_gb + 1} GB (${s.memory_gb} GB game + 1 GB Java overhead)`;
  const pct = 100 * s.disk_used / (s.disk_gb * 1e9);
  $("mcs-disk").textContent = sizeText(s.disk_used);
  $("mcs-disk-bar").style.width = `${Math.min(100, pct)}%`;
  $("mcs-disk-bar").classList.toggle("high", pct >= 85);
  $("mcs-disk-limit").textContent = `of ${s.disk_gb} GB limit`;
  const shown = s.public && s.public_address ? s.public_address : s.vpn_address;
  $("mcs-address-text").textContent = shown;
  $("mcs-address-title").textContent = s.public ? "Server address for friends" : "Server address (VPN only)";
  $("mcs-address-help").textContent = s.public
    ? `Players add this in Minecraft Java ${s.version} under Multiplayer, Add Server.` + (s.public_ip ? ` It always follows your home address (now ${s.public_ip}).` : "")
    : "Only devices on your VPN can join. Switch on Open to friends below to get an address anyone can use.";
  $("mcs-host-check").hidden = !s.host_check;
  if (s.host_check) $("mcs-host-check").replaceChildren(make("span", `dot ${s.host_check.ok ? "on" : "rest"}`), document.createTextNode(s.host_check.text));
  $("mcs-public").checked = s.public;
  $("mcs-address").textContent = s.public
    ? `Friends join with your home address and port ${s.port}. You can also use ${s.vpn_address} over the VPN.`
    : `Only reachable over the VPN at ${s.vpn_address}. Switch on to let friends join from their homes; this opens port ${s.port} on the router.`;
  document.querySelectorAll("[data-mc-action]").forEach((b) => {
    const a = b.dataset.mcAction;
    b.disabled = s.state === "installing" || (a === "start" ? s.state !== "stopped" : s.state === "stopped");
  });
}

async function refreshDetail() {
  try {
    renderDetail(await api(`/api/mc/s/${enc(mcSlug)}`));
  } catch (e) {
    toast(e.message);
  }
}

async function refreshLog() {
  try {
    const { text } = await api(`/api/mc/s/${enc(mcSlug)}/log`);
    if (text === mcLogKey) return;
    mcLogKey = text;
    const pre = $("mcs-log");
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = text || "Nothing logged yet.";
    if (atBottom || !pre.dataset.seen) pre.scrollTop = pre.scrollHeight;
    pre.dataset.seen = "1";
  } catch (e) {
    if (!mcLogKey) $("mcs-log").textContent = `Couldn't load the log: ${failText(e)}`;
  }
}

document.querySelectorAll("[data-mc-action]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/action`, { action: b.dataset.mcAction });
    toast(`${b.textContent}ing…`.replace("Stoping", "Stopping"));
  } catch (e) {
    toast(e.message);
  }
  refreshDetail();
}));

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  area.setSelectionRange(0, text.length);
  const ok = document.execCommand("copy");
  area.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error("copy"));
}

$("mcs-copy").addEventListener("click", () => {
  const text = $("mcs-address-text").textContent;
  copyText(text).then(() => toast(`Copied ${text}`)).catch(() => toast("Couldn't copy; press and hold the address to copy it."));
});

$("mcs-public").addEventListener("change", async (e) => {
  const on = e.target.checked;
  e.target.checked = !on;
  const question = on
    ? `Open ${mcInfo.name} to friends? This forwards port ${mcInfo.port} on your router to this server. Only whitelisted players can join.`
    : `Close ${mcInfo.name} to friends? This removes its router forward.`;
  if (!(await mcAsk(e.target.closest(".card"), question, on ? "Yes, open it" : "Yes, close it", false))) return;
  e.target.checked = on;
  e.target.disabled = true;
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/public`, { on });
    toast(on ? "Open to friends." : "Closed to friends.");
  } catch (err) {
    e.target.checked = !on;
    toast(err.message);
  } finally {
    e.target.disabled = false;
  }
  refreshDetail();
});

document.querySelectorAll("#mcs-cmd-kind button").forEach((b) => b.addEventListener("click", () => {
  mcCmdKind = b.dataset.kind;
  document.querySelectorAll("#mcs-cmd-kind button").forEach((x) => x.classList.toggle("active", x === b));
  $("mcs-cmd-text").placeholder = mcCmdKind === "chat" ? "Say something to everyone" : "Command, like time set day";
}));
$("mcs-cmd").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("mcs-cmd-text").value.trim();
  if (!text) return;
  $("mcs-cmd-text").value = "";
  try {
    const out = await postJson(`/api/mc/s/${enc(mcSlug)}/command`, { text: text.replace(/^\//, ""), chat: mcCmdKind === "chat" });
    if (out.reply) toast(out.reply);
  } catch (err) {
    toast(err.message);
  }
  setTimeout(refreshLog, 600);
});

async function loadFiles(path) {
  mcPath = path;
  $("mcs-editor").hidden = true;
  try {
    const listing = await api(`/api/mc/s/${enc(mcSlug)}/files?path=${enc(path)}`);
    const crumbs = $("mcs-crumbs");
    const parts = listing.path ? listing.path.split("/") : [];
    const home = make("button", "ghost", "server");
    home.addEventListener("click", () => loadFiles(""));
    crumbs.replaceChildren(home, ...parts.flatMap((p, i) => {
      const b = make("button", "ghost", p);
      b.addEventListener("click", () => loadFiles(parts.slice(0, i + 1).join("/")));
      return [make("span", "muted", "/"), b];
    }));
    $("mcs-entries").replaceChildren(...(listing.entries.length ? listing.entries.map((entry) => {
      const li = make("li");
      const full = listing.path ? `${listing.path}/${entry.name}` : entry.name;
      const open = make("button", "grow file");
      open.type = "button";
      const label = make("span", "file-text", entry.name);
      label.append(make("small", null, entry.dir ? entry.modified.replace("T", " ") : `${bytes(entry.size)} · ${entry.modified.replace("T", " ")}`));
      open.append(fileIcon(entry.dir ? "folder" : "doc"), label);
      open.addEventListener("click", () => (entry.dir ? loadFiles(full) : openFile(full)));
      const del = trashButton(`Delete ${entry.name}`);
      del.addEventListener("click", async () => {
        if (!(await mcAsk($("mcs-entries"), `Delete ${entry.name}${entry.dir ? " and everything in it" : ""}? This can't be undone.`, "Yes, delete", true))) return;
        try { await postJson(`/api/mc/s/${enc(mcSlug)}/file`, { op: "delete", path: full }); } catch (err) { toast(err.message); }
        loadFiles(mcPath);
      });
      li.append(open, del);
      return li;
    }) : [make("li", "empty-row", "This folder is empty.")]));
  } catch (e) {
    toast(e.message);
    if (path) loadFiles("");
  }
}

async function openFile(path) {
  try {
    const file = await api(`/api/mc/s/${enc(mcSlug)}/files?path=${enc(path)}`);
    mcEditing = path;
    $("mcs-editor").hidden = false;
    $("mcs-edit-name").textContent = path.split("/").pop();
    $("mcs-edit-download").href = `/api/mc/s/${enc(mcSlug)}/download?path=${enc(path)}`;
    $("mcs-edit-text").hidden = file.text == null;
    $("mcs-edit-save").hidden = file.text == null;
    $("mcs-edit-text").value = file.text ?? "";
    if (file.text == null) toast(`${bytes(file.size)} file; it isn't text, so it can only be downloaded.`);
    $("mcs-editor").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    toast(e.message);
  }
}

$("mcs-edit-save").addEventListener("click", async () => {
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/file`, { op: "write", path: mcEditing, text: $("mcs-edit-text").value });
    toast("Saved.");
  } catch (e) {
    toast(e.message);
  }
});
$("mcs-edit-close").addEventListener("click", () => { $("mcs-editor").hidden = true; });
$("mcs-mkdir").addEventListener("click", () => {
  $("mcs-mkdir-form").hidden = false;
  $("mcs-mkdir-name").focus();
});
$("mcs-mkdir-cancel").addEventListener("click", () => {
  $("mcs-mkdir-form").hidden = true;
  $("mcs-mkdir-name").value = "";
});
$("mcs-mkdir-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("mcs-mkdir-name").value.trim();
  if (!name) return;
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/file`, { op: "mkdir", path: mcPath ? `${mcPath}/${name}` : name });
    $("mcs-mkdir-name").value = "";
    $("mcs-mkdir-form").hidden = true;
  } catch (err) {
    toast(err.message);
  }
  loadFiles(mcPath);
});
$("mcs-upload").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  for (const [i, file] of files.entries()) {
    $("mcs-upload-state").textContent = `Uploading ${file.name} (${i + 1} of ${files.length})…`;
    try {
      await api(`/api/mc/s/${enc(mcSlug)}/upload?dir=${enc(mcPath)}&name=${enc(file.name)}`,
        { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file });
    } catch (err) {
      toast(`${file.name}: ${err.message}`);
    }
  }
  $("mcs-upload-state").textContent = files.length ? `Uploaded ${files.length} file${files.length > 1 ? "s" : ""}. Restart the server to load new mods.` : "";
  e.target.value = "";
  loadFiles(mcPath);
});

async function loadMods() {
  if (!mcInfo) return;
  const vanilla = mcInfo.loader === "vanilla";
  serverPicked = new Map(mcInfo.mods.map((m) => [m, m.includes(":") ? `${m.split(":")[0]} (test build)` : m]));
  serverModsKey = JSON.stringify([...serverPicked.keys()].sort());
  renderPicked("mcs", serverPicked, markModsDirty);
  $("mcs-mods-save").hidden = true;
  $("mcs-search").disabled = vanilla;
  $("mcs-search").placeholder = vanilla ? "Vanilla servers don't take mods" : "Search Modrinth to add";
  if (!vanilla) searchMods("mcs", true);
  try {
    const { folder, files } = await api(`/api/mc/s/${enc(mcSlug)}/modfiles`);
    $("mcs-jar-title").textContent = folder ? `Files in the ${folder} folder` : "No mods folder";
    $("mcs-jars").replaceChildren(...(files.length ? files.map((f) => {
      const li = make("li");
      const del = trashButton(`Delete ${f}`);
      del.addEventListener("click", async () => {
        if (!(await mcAsk($("mcs-jars"), `Delete ${f}? Restart the server afterwards.`, "Yes, delete", true))) return;
        try { await postJson(`/api/mc/s/${enc(mcSlug)}/file`, { op: "delete", path: `${folder}/${f}` }); } catch (err) { toast(err.message); }
        loadMods();
      });
      li.append(make("span", "grow", f), del);
      return li;
    }) : [make("li", "empty-row", "None yet. Mods picked above download on the next start.")]));
  } catch (e) {
    $("mcs-jars").replaceChildren(make("li", "empty-row", `Couldn't list the mods folder: ${failText(e)}`));
  }
}

$("mcs-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => searchMods("mcs", true), 350); });
$("mcs-mods-save").addEventListener("click", async () => {
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/settings`, { mods: [...serverPicked.keys()] });
    toast("Mods saved. The server is restarting to download them.");
    await refreshDetail();
    loadMods();
  } catch (e) {
    toast(e.message);
  }
});

function fillSettings() {
  $("mcx-motd").value = mcInfo.motd;
  $("mcx-host").value = mcInfo.host || "";
  $("mcx-host-help").textContent = "A DuckDNS name: create it on duckdns.org first and the hub keeps it pointed at home. Your own domain: at its "
    + "DNS settings add a CNAME record for the name pointing to your home DuckDNS name"
    + (mcInfo.port !== 25565 ? `, plus an SRV record _minecraft._tcp for that name with port ${mcInfo.port}, so friends don't need to type the port.` : ".")
    + " Leave empty to use your home address.";
  $("mcx-memory").value = mcInfo.memory_gb;
  $("mcx-memory-label").textContent = `${mcInfo.memory_gb} GB`;
  $("mcx-players").value = mcInfo.max_players;
  $("mcx-disk").value = mcInfo.disk_gb;
  $("mcx-port").value = mcInfo.port;
  $("mcx-difficulty").value = mcInfo.difficulty;
  $("mcx-mode").value = mcInfo.mode;
  renderWhitelist(mcInfo.whitelist, mcInfo.operators);
  $("mcs-wl-on").checked = mcInfo.whitelist_on;
  $("mcs-wl-state").textContent = mcInfo.whitelist_on
    ? "On: only the players below can join."
    : mcInfo.public ? "Off: anyone with a Minecraft account who has your address can join." : "Off: anyone on your VPN can join.";
}

$("mcs-wl-on").addEventListener("change", async (e) => {
  const on = e.target.checked;
  if (!on && mcInfo.public) {
    e.target.checked = true;
    const question = `${mcInfo.name} is open to friends. With the whitelist off, anyone with a Minecraft account who finds your address can join. Turn it off?`;
    if (!(await mcAsk($("mcs-wl-state"), question, "Yes, turn it off", true))) return;
    e.target.checked = false;
  }
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/settings`, { whitelist: on });
    toast(on ? "Whitelist on. The server is restarting." : "Whitelist off. The server is restarting.");
    await refreshDetail();
    fillSettings();
  } catch (err) {
    e.target.checked = !on;
    toast(err.message);
  }
});

$("mcx-memory").addEventListener("input", () => { $("mcx-memory-label").textContent = `${$("mcx-memory").value} GB`; });
$("mcs-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/settings`, { host: $("mcx-host").value.trim(), motd: $("mcx-motd").value, memory_gb: Number($("mcx-memory").value),
      max_players: Number($("mcx-players").value), disk_gb: Number($("mcx-disk").value), port: Number($("mcx-port").value), difficulty: $("mcx-difficulty").value, mode: $("mcx-mode").value });
    toast("Saved.");
    refreshDetail();
  } catch (err) {
    toast(err.message);
  }
});
function renderWhitelist(names, operators) {
  $("mcs-wl-list").replaceChildren(...(names.length ? names.map((n) => {
    const li = make("li");
    const remove = make("button", "row-btn", "Remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${n} from the whitelist`);
    remove.addEventListener("click", () => changeWhitelist(n, false));
    const name = make("span", "grow", n);
    if (operators.includes(n)) name.append(make("span", "tag op", "Operator"));
    li.append(name, remove);
    return li;
  }) : [make("li", "empty-row", "Nobody yet, so nobody can join.")]));
}

async function changeWhitelist(name, add) {
  try {
    const out = await postJson(`/api/mc/s/${enc(mcSlug)}/whitelist`, { name, add });
    toast(out.reply);
    renderWhitelist(out.whitelist, (mcInfo || {}).operators || []);
    refreshDetail();
  } catch (err) {
    toast(err.message);
  }
}

$("mcs-whitelist").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("mcs-wl-name").value.trim();
  if (!/^[A-Za-z0-9_]{3,16}$/.test(name)) { toast("Minecraft usernames are 3 to 16 letters, numbers or _."); return; }
  $("mcs-wl-name").value = "";
  changeWhitelist(name, true);
});
$("mcs-delete").addEventListener("click", async () => {
  const typed = $("mcs-del-name").value;
  if (typed !== mcInfo.name) { toast("Type the server's exact name first."); return; }
  try {
    await postJson(`/api/mc/s/${enc(mcSlug)}/action`, { action: "delete", confirm: typed });
    toast(`${typed} deleted. Its world is kept for 7 days in case you change your mind, then removed for good.`);
    location.hash = "mc";
  } catch (e) {
    toast(e.message);
  }
});

async function refreshRouter() {
  try {
    const { forwards } = await api("/api/router/forwards");
    $("sub-router").textContent = `${forwards.length} port forward${forwards.length === 1 ? "" : "s"}`;
    if (current !== "router") return;
    $("router-forwards").replaceChildren(...(forwards.length ? forwards.map((f) => {
      const li = make("li");
      const text = make("span", "grow", f.description || "(no name)");
      text.append(make("small", null, `${f.protocol} ${f.external_port}${f.internal_port !== f.external_port ? ` → ${f.internal_port}` : ""} to ${f.client}${f.enabled ? "" : " · off"}`));
      li.append(text, make("span", f.ours ? "chip" : "chip muted", f.ours ? "hub" : "router"));
      return li;
    }) : [make("li", "empty-row", "No port forwards.")]));
  } catch (e) {
    $("sub-router").textContent = "Not answering";
    if (current === "router") $("router-forwards").replaceChildren(make("li", "empty-row", e.message));
  }
}

viewHooks.push((view, parts) => {
  clearInterval(mcTimer);
  mcTimer = null;
  if (view !== "mcserver" && mcAnswer) mcAnswer(false);
  if (view === "main" || view === "computers") {
    if (!$("bubble-mc").hidden) refreshMcList();
    if (!$("bubble-router").hidden) refreshRouter();
  }
  if (view === "mc") {
    refreshMcList();
    mcTimer = setInterval(refreshMcList, 5000);
  }
  if (view === "router") refreshRouter();
  if (view === "mcnew") {
    $("mcf-form").reset();
    $("mcf-memory-label").textContent = "4 GB";
    formLoader = "paper";
    document.querySelectorAll("#mcf-loader button").forEach((x) => x.classList.toggle("active", x.dataset.loader === "paper"));
    formPicked = new Map();
    renderPicked("mcf", formPicked, () => {});
    loadVersions();
  }
  if (view === "mcserver") {
    const slug = parts[1];
    if (slug !== mcSlug) { mcInfo = null; mcPath = ""; }
    mcSlug = slug;
    refreshDetail().then(() => showTab(parts[2] || "overview"));
    mcTimer = setInterval(() => {
      if (mcTab === "overview") refreshDetail();
      if (mcTab === "console") refreshLog();
    }, 3000);
  }
});
