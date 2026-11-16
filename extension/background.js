// Right-click menus, the toolbar badge and notifications.
import { activeCounts, send, state } from "./harbor.js";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "harbor-link", title: "Send link to Harbor", contexts: ["link"] });
    chrome.contextMenus.create({ id: "harbor-page", title: "Send this page to Harbor", contexts: ["page"] });
  });
  chrome.alarms.create("harbor-badge", { periodInMinutes: 0.5 });
  updateBadge();
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("harbor-badge", { periodInMinutes: 0.5 });
  updateBadge();
});
chrome.alarms.onAlarm.addListener(a => { if (a.name === "harbor-badge") updateBadge(); });

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.menuItemId === "harbor-link" ? info.linkUrl : tab?.url;
  await sendAndTell([url]);
});

// The popup asks the worker to refresh the badge after it sends something.
chrome.runtime.onMessage.addListener(msg => { if (msg === "badge") updateBadge(); });

export async function sendAndTell(urls) {
  try {
    const r = await send(urls);
    notify("Sent to Harbor", r.added ? `Queued ${r.added} download(s)` + (r.duplicates ? `, ${r.duplicates} already queued` : "") : "Already in Harbor's queue");
  } catch (e) {
    notify("Harbor", e.message);
  }
  updateBadge();
}

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/128.png", title, message });
}

async function updateBadge() {
  try {
    const c = activeCounts(await state());
    const n = c.downloads + c.conversions;
    await chrome.action.setBadgeBackgroundColor({ color: "#2f6fed" });
    await chrome.action.setBadgeText({ text: n ? String(n) : "" });
    await chrome.action.setTitle({ title: `Harbor: ${c.downloads} download(s), ${c.conversions} conversion(s) active` });
  } catch {
    await chrome.action.setBadgeBackgroundColor({ color: "#888888" });
    await chrome.action.setBadgeText({ text: "off" });
    await chrome.action.setTitle({ title: "Harbor isn't running" });
  }
}
