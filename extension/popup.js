import { ARCHIVE_RE, DEFAULT_URL, activeCounts, send, settings, state } from "./harbor.js";

const $ = id => document.getElementById(id);
let pageLinks = [];

function say(text, kind) { $("msg").textContent = text; $("msg").className = "msg" + (kind ? " " + kind : ""); }
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

/* ---------- status ---------- */
async function showStatus() {
  try {
    const st = await state();
    const c = activeCounts(st);
    $("status").textContent = "Running";
    $("status").className = "status ok";
    $("chips").innerHTML =
      `<span class="chip">downloads <b>${c.downloads}</b></span><span class="chip">conversions <b>${c.conversions}</b></span>` +
      (st.downloads_paused || st.paused ? `<span class="chip"><b>paused</b></span>` : "");
    const rows = [
      ...st.downloads.filter(j => j.state === "running" || j.state === "queued").map(j => ({
        name: j.name, info: j.state === "running" ? (j.total ? Math.round(100 * j.done / j.total) + "%" : "downloading") : "queued",
      })),
      ...st.jobs.filter(j => j.state === "running" || j.state === "queued").map(j => ({
        name: j.name, info: j.state === "running" ? `converting ${j.done}/${j.total}` : "to convert",
      })),
    ].slice(0, 5);
    $("active").innerHTML = rows.map(r => `<li><span>${esc(r.name)}</span><span>${esc(r.info)}</span></li>`).join("");
    $("dest").placeholder = st.harbor?.download_dir ? `Harbor's default: ${st.harbor.download_dir}` : "Required: Harbor has no default folder";
    $("sendTab").disabled = false;
  } catch (e) {
    $("status").textContent = e.signedOut ? "Not signed in" : "Not running";
    $("status").className = "status bad";
    $("chips").innerHTML = "";
    $("active").innerHTML = `<li><span>${esc(e.message)}</span></li>`;
  }
}

/* ---------- links on the page ---------- */
async function loadLinks() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => [...document.querySelectorAll("a[href]")].map(a => ({
        url: a.href, text: (a.textContent || a.title || "").trim().slice(0, 120),
      })),
    });
    const seen = new Set();
    pageLinks = result.filter(l => /^https?:/i.test(l.url) && !seen.has(l.url) && seen.add(l.url));
  } catch {
    pageLinks = null; // chrome:// pages, the Web Store, PDFs viewer, etc. can't be read
  }
  renderLinks();
}

function renderLinks() {
  if (pageLinks === null) {
    $("links").innerHTML = `<li class="empty">Chrome doesn't let extensions read this page.</li>`;
    return updateSelected();
  }
  const all = $("allLinks").checked;
  const shown = all ? pageLinks : pageLinks.filter(l => ARCHIVE_RE.test(l.url));
  $("links").innerHTML = shown.length
    ? shown.map((l, i) => {
        const name = decodeURIComponent(new URL(l.url).pathname.split("/").pop() || l.url);
        return `<li><label><input type="checkbox" data-url="${esc(l.url)}"><span><div class="name">${esc(l.text || name)}</div><div class="url">${esc(l.url)}</div></span></label></li>`;
      }).join("")
    : `<li class="empty">No ${all ? "" : "archive "}links on this page.${all ? "" : " Tick “all links” to see every link."}</li>`;
  updateSelected();
}

function selectedUrls() { return [...$("links").querySelectorAll("input:checked")].map(i => i.dataset.url); }
function updateSelected() {
  const n = selectedUrls().length;
  $("sendSelected").disabled = !n;
  $("sendSelected").textContent = n ? `Send ${n} selected` : "Send selected";
}

/* ---------- sending ---------- */
async function doSend(urls) {
  say("Sending…");
  try {
    const r = await send(urls);
    say(r.added ? `Queued ${r.added} download(s)` + (r.duplicates ? `, ${r.duplicates} already queued` : "") : "Already in Harbor's queue", "ok");
    chrome.runtime.sendMessage("badge");
    showStatus();
  } catch (e) {
    say(e.message, "err");
    if (/save-to folder/.test(e.message)) $("settingsBox").open = true;
  }
}

$("sendTab").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  doSend([tab.url]);
};
$("sendSelected").onclick = () => doSend(selectedUrls());
$("links").onchange = updateSelected;
$("allLinks").onchange = renderLinks;
$("selectAll").onclick = () => {
  const boxes = [...$("links").querySelectorAll("input[type=checkbox]")];
  const on = boxes.some(b => !b.checked);
  boxes.forEach(b => { b.checked = on; });
  updateSelected();
};
$("openHarbor").onclick = async () => chrome.tabs.create({ url: (await settings()).harborUrl + "/" });

/* ---------- settings ---------- */
(async () => {
  const s = await settings();
  $("harborUrl").value = s.harborUrl === DEFAULT_URL ? "" : s.harborUrl;
  $("dest").value = s.dest;
  $("convert").checked = s.convert;
  $("token").value = s.token;
  if (!s.token) $("settingsBox").open = true;
})();
$("harborUrl").onchange = async () => {
  const v = $("harborUrl").value.trim();
  if (v && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?\/?$/.test(v)) {
    return say("Harbor runs on this computer: use http://localhost:<port>", "err");
  }
  await chrome.storage.local.set({ harborUrl: v || DEFAULT_URL });
  say("");
  showStatus();
};
$("dest").onchange = () => chrome.storage.local.set({ dest: $("dest").value.trim() });
$("token").onchange = async () => {
  const v = $("token").value.trim();
  if (v && !/^hbt_[A-Za-z0-9_-]{20,}$/.test(v)) return say("That doesn't look like a Harbor token (hbt_…)", "err");
  await chrome.storage.local.set({ token: v });
  say("");
  showStatus();
};
$("convert").onchange = () => chrome.storage.local.set({ convert: $("convert").checked });

showStatus();
loadLinks();
