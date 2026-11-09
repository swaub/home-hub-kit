let pkTimer = null;

function ago(iso) {
  if (!iso) return "not checked yet";
  const minutes = Math.round((Date.now() - new Date(iso)) / 60000);
  return minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
}

function packageCard(p) {
  const card = make("div", "card package");
  const head = make("div", "card-head");
  head.append(make("h2", null, p.item || p.merchant || "Package"), make("span", `state ${p.status}`, p.label));
  card.append(head);
  const meta = [p.merchant, p.carrier && p.carrier !== p.merchant ? p.carrier : ""].filter(Boolean).join(" · ");
  if (meta) card.append(make("p", "meta", meta));
  if (p.eta && p.status !== "delivered") card.append(make("p", "eta", `Arriving ${p.eta}`));
  if (p.status === "delivered" && p.delivered_at) card.append(make("p", "eta", `Delivered ${new Date(p.delivered_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`));
  if (p.tracking) card.append(make("p", "number", p.tracking));
  const buttons = make("div", "buttons");
  if (p.url) {
    const track = make("a", null, p.carrier === "Amazon" || p.merchant === "Amazon" ? "Open in Amazon" : `Track with ${p.carrier || "carrier"}`);
    track.href = p.url;
    track.target = "_blank";
    track.rel = "noopener";
    buttons.append(track);
  }
  const hide = make("button", null, "Hide");
  hide.addEventListener("click", async () => {
    try { renderPackages(await postJson("/api/packages", { action: "hide", key: p.key })); } catch (e) { toast(e.message); }
  });
  buttons.append(hide);
  card.append(buttons);
  return card;
}

function renderPackages(s) {
  const coming = s.packages.filter((p) => p.status !== "delivered");
  const today = coming.filter((p) => p.status === "out_for_delivery").length;
  $("sub-packages").textContent = !s.ready && !s.packages.length ? "Connect Gmail" :
    coming.length ? `${coming.length} on the way${today ? ` · ${today} today` : ""}` : "Nothing on the way";
  if (current !== "packages") return;
  $("pk-connect").hidden = s.ready;
  $("pk-checked").textContent = s.error ? `Last check failed: ${s.error}` : `Email checked ${ago(s.last_scan)}`;
  const delivered = s.packages.filter((p) => p.status === "delivered");
  const parts = coming.map(packageCard);
  if (delivered.length) parts.push(make("p", "muted section-label", "Delivered recently"), ...delivered.map(packageCard));
  if (!parts.length) parts.push(make("p", "card muted", s.ready ? "Nothing on the way right now." : "Connect Gmail above to see your packages."));
  $("pk-list").replaceChildren(...parts);
}

async function refreshPackages() {
  try {
    renderPackages(await api("/api/packages"));
  } catch (e) {
    $("sub-packages").textContent = "Not answering";
    if (current === "packages" && !$("pk-list").querySelector(".package")) $("pk-list").replaceChildren(make("p", "card muted", `Couldn't load packages: ${failText(e)}`));
  }
}

$("pk-signin").addEventListener("click", async () => {
  try { window.open((await postJson("/api/packages", { action: "connect_url" })).url, "_blank", "noopener"); } catch (e) { toast(e.message); }
});
$("pk-connect-go").addEventListener("click", async () => {
  const address = $("pk-address").value.trim();
  if (!address) { toast("Paste the address the sign-in page ended on."); return; }
  $("pk-connect-go").disabled = true;
  try {
    const out = await postJson("/api/packages", { action: "connect", address });
    toast(`Gmail connected. Found ${out.found} shipping email${out.found === 1 ? "" : "s"}.`);
    $("pk-address").value = "";
    renderPackages(out);
  } catch (e) {
    toast(e.message);
  } finally {
    $("pk-connect-go").disabled = false;
  }
});
$("pk-refresh").addEventListener("click", async () => {
  toast("Checking email…");
  try {
    const out = await postJson("/api/packages", { action: "refresh" });
    toast(out.found ? `Found ${out.found} new update${out.found === 1 ? "" : "s"}.` : "No new shipping emails.");
    renderPackages(out);
  } catch (e) {
    toast(e.message);
  }
});
$("pk-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    renderPackages(await postJson("/api/packages", { action: "add", number: $("pk-number").value, what: $("pk-what").value }));
    $("pk-number").value = "";
    $("pk-what").value = "";
  } catch (err) {
    toast(err.message);
  }
});

viewHooks.push((view) => {
  clearInterval(pkTimer);
  if (view === "packages" || ((view === "main" || view === "household") && !$("bubble-packages").hidden)) refreshPackages();
  if (view === "packages") pkTimer = setInterval(refreshPackages, 30000);
});
