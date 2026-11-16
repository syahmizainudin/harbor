// Talking to the local Harbor server. Shared by the background worker and the popup.
//
// Harbor accepts these requests because they come from this extension's fixed origin
// (chrome-extension://<id>, set by Chrome). The API key is never handled here: Harbor uses the
// key from its .env file when a download arrives without one.

export const DEFAULT_URL = "http://localhost:8765";
export const ARCHIVE_RE = /\.(zip|cbz|cbr|rar|7z|cb7|pdf|epub)(?:$|[?#])/i;

export async function settings() {
  const s = await chrome.storage.local.get({ harborUrl: DEFAULT_URL, dest: "", convert: true });
  s.harborUrl = s.harborUrl.replace(/\/+$/, "") || DEFAULT_URL;
  return s;
}

async function call(path, body) {
  const { harborUrl } = await settings();
  let r;
  try {
    r = await fetch(harborUrl + path, body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(`Harbor isn't running at ${harborUrl}`);
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 403) throw new Error("Harbor refused this extension (check HARBOR_EXTENSION_ORIGINS)");
  if (!r.ok) throw new Error(data.error || `Harbor answered ${r.status}`);
  return data;
}

export const state = () => call("/api/state");

/** Queue URLs in Harbor's download queue, with Harbor's own defaults. Returns {added, duplicates}. */
export async function send(urls) {
  urls = [...new Set(urls.filter(u => /^https?:\/\//i.test(u)))];
  if (!urls.length) throw new Error("No http(s) links to send");
  const [st, s] = await Promise.all([state(), settings()]);
  const h = st.harbor || {};
  const dest = s.dest || h.download_dir;
  if (!dest) throw new Error("Choose a save-to folder in the Harbor popup, or set HARBOR_DOWNLOAD_DIR");
  const mode = h.auth_mode || "none";
  if (mode !== "none" && !h.has_key) {
    throw new Error("Harbor has no API key in its .env (HARBOR_API_KEY), or set HARBOR_AUTH_MODE=none");
  }
  return call("/api/download/add", {
    urls,
    dest,
    auth: { mode, name: h.auth_name || "", key: "" },
    after: {
      convert: s.convert,
      quality: st.defaults?.quality ?? 80,
      method: st.defaults?.method ?? 4,
      delete_original: h.delete_original ?? true,
    },
  });
}

/** Active (queued or running) counts. */
export function activeCounts(st) {
  const active = j => j.state === "queued" || j.state === "running";
  return { downloads: st.downloads.filter(active).length, conversions: st.jobs.filter(active).length };
}
