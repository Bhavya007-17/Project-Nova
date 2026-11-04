// Nova's service worker (SPEC §9). One native-messaging port to Nova (host "com.nova.bridge": a small
// Python relay to Nova's named pipe). Tab operations run here; page operations run in each frame's
// content script (content.js), reached with chrome.scripting.executeScript so every frame answers with
// its frameId. While the port is open Chrome keeps this worker alive; when Nova isn't running the host
// exits, and the worker tries again on tab / focus events, after a disconnect, and on a 30 s alarm.
"use strict";

const HOST = "com.nova.bridge";
const VERSION = chrome.runtime.getManifest().version;
const RETRY_MS = [1600, 3000, 6000, 12000, 20000]; // after a disconnect (Nova restarting); then the 30 s alarm
const MIN_GAP_MS = 1500; // every try starts a process: never more often than this
const STEADY_MS = 5000; // a connection that lasted this long was a real one: retry quickly when it drops
const NAV_WAIT_MS = 1500;
const LATE_NAV_MS = 150; // after a click the page didn't react to: a navigation that's only starting

let port = null;
let retry = 0;
let retryTimer = null;
let lastTry = 0;
let connectedAt = 0;
let focusedWindow = chrome.windows.WINDOW_ID_NONE;

// --- the port -------------------------------------------------------------------------------------------------

function connect() {
  if (port) return;
  const now = Date.now();
  if (now - lastTry < MIN_GAP_MS) return;
  lastTry = now;
  let p;
  try {
    p = chrome.runtime.connectNative(HOST);
  } catch (e) {
    schedule();
    return;
  }
  port = p;
  connectedAt = now;
  p.onMessage.addListener((msg) => {
    retry = 0; // Nova answered: it's there
    onMessage(p, msg);
  });
  p.onDisconnect.addListener(() => {
    void chrome.runtime.lastError; // read it, or Chrome logs "Unchecked runtime.lastError"
    if (port === p) port = null;
    if (Date.now() - connectedAt > STEADY_MS) retry = 0;
    schedule();
  });
  hello(p);
}

function schedule() {
  if (retryTimer !== null || retry >= RETRY_MS.length) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    retry += 1;
    connect();
  }, RETRY_MS[retry]);
}

function send(msg) {
  if (!port) return false;
  try {
    port.postMessage(msg);
    return true;
  } catch (e) {
    return false;
  }
}

async function hello(p) {
  const tab = await activeTab().catch(() => null);
  try {
    p.postMessage({ event: "hello", version: VERSION, focused: focusedWindow !== chrome.windows.WINDOW_ID_NONE, tab: info(tab) });
  } catch (e) {
    /* the host already exited (Nova isn't running) */
  }
}

// --- what Nova keeps in mind about the browser: the focused window's active tab ------------------------------

function info(t) {
  if (!t) return null;
  return {
    id: t.id, windowId: t.windowId, index: t.index, url: t.url || t.pendingUrl || "", title: t.title || "",
    active: !!t.active, status: t.status || "", incognito: !!t.incognito, lastAccessed: t.lastAccessed || 0,
  };
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

async function report(tab) {
  if (!port) {
    connect();
    return;
  }
  send({ event: "tab", focused: focusedWindow !== chrome.windows.WINDOW_ID_NONE, tab: info(tab || (await activeTab().catch(() => null))) });
}

chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
chrome.alarms.create("nova-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "nova-reconnect" && !port) {
    retry = 0;
    connect();
  }
});
chrome.windows.onFocusChanged.addListener((windowId) => {
  focusedWindow = windowId;
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    send({ event: "focus", focused: false });
    return;
  }
  report(null);
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId).then((tab) => report(tab), () => {});
});
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (!tab.active || !("url" in change || "title" in change || change.status === "complete")) return;
  if (focusedWindow !== chrome.windows.WINDOW_ID_NONE && tab.windowId !== focusedWindow) return;
  report(tab);
});
chrome.windows.getLastFocused().then((w) => {
  if (w && w.focused) focusedWindow = w.id;
}, () => {});
connect();

// --- requests from Nova -----------------------------------------------------------------------------------------

async function onMessage(p, msg) {
  if (!msg || typeof msg !== "object" || msg.id === undefined) return;
  const started = performance.now();
  let reply;
  try {
    const handler = COMMANDS[msg.cmd];
    if (!handler) throw new Error(`unknown command ${msg.cmd}`);
    const result = await handler(msg.args || {});
    reply = { id: msg.id, ok: true, result, ms: Math.round(performance.now() - started) };
  } catch (e) {
    reply = { id: msg.id, ok: false, error: String((e && e.message) || e), ms: Math.round(performance.now() - started) };
  }
  try {
    p.postMessage(reply);
  } catch (e) {
    /* Nova went away mid-request */
  }
}

async function tabFor(args) {
  if (args.tabId !== undefined && args.tabId !== null) return chrome.tabs.get(args.tabId);
  const tab = await activeTab();
  if (!tab) throw new Error("no Chrome tab is open");
  return tab;
}

// Run __nova[fn](...args) in some frames of a tab (content.js; injected first where it isn't loaded yet:
// tabs that were open before the extension). Returns [{frameId, result}].
function runNova(fn, args) {
  const nova = globalThis.__nova;
  if (!nova) return { __missing: true };
  return nova[fn](...args);
}

async function inFrames(tabId, frameIds, fn, args) {
  const target = frameIds ? { tabId, frameIds } : { tabId, allFrames: true };
  let results = await chrome.scripting.executeScript({ target, func: runNova, args: [fn, args] });
  const missing = results.filter((r) => r.result && r.result.__missing).map((r) => r.frameId);
  if (missing.length) {
    await chrome.scripting.executeScript({ target: { tabId, frameIds: missing }, files: ["content.js"] });
    const again = await chrome.scripting.executeScript({ target: { tabId, frameIds: missing }, func: runNova, args: [fn, args] });
    results = results.filter((r) => !missing.includes(r.frameId)).concat(again);
  }
  return results;
}

function splitId(id) {
  const [frame, n] = String(id).split(":");
  return [Number(frame), Number(n)];
}

// Resolves "navigated" when the tab starts loading another page or changes its address; else null after `ms`.
function navigation(tabId, ms) {
  let done;
  const promise = new Promise((resolve) => {
    const timer = setTimeout(() => finish(null), ms);
    function listener(id, change) {
      if (id === tabId && (change.status === "loading" || "url" in change)) finish("navigated");
    }
    function finish(value) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(value);
    }
    done = finish;
    chrome.tabs.onUpdated.addListener(listener);
  });
  return { promise, cancel: () => done(null) };
}

function waitTab(tabId, { status = "complete", urlPrefix = "", timeout = 10000 } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    let settled = false;
    const finish = (tab, timedOut) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve({ tab: info(tab), timedOut, ms: Date.now() - started });
    };
    const ok = (tab) => tab && (!status || tab.status === status) && (!urlPrefix || (tab.url || "").startsWith(urlPrefix));
    const listener = (id, change, tab) => {
      if (id === tabId && ok(tab)) finish(tab, false);
    };
    const timer = setTimeout(() => chrome.tabs.get(tabId).then((t) => finish(t, true), () => finish(null, true)), timeout);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (ok(tab)) finish(tab, false);
    }, () => finish(null, true));
  });
}

async function focusTab(tab) {
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
  return info(await chrome.tabs.get(tab.id));
}

const COMMANDS = {
  ping: async () => ({ version: VERSION }),

  // --- tabs ---
  "tabs.list": async () => {
    const tabs = await chrome.tabs.query({ windowType: "normal" });
    const active = await activeTab().catch(() => null);
    return { tabs: tabs.map(info), activeId: active ? active.id : null, focused: focusedWindow !== chrome.windows.WINDOW_ID_NONE };
  },
  "tabs.active": async () => ({ tab: info(await activeTab()) }),
  "tabs.activate": async ({ tabId }) => ({ tab: await focusTab(await chrome.tabs.get(tabId)) }),
  "tabs.open": async ({ url, active = true }) => {
    const current = await activeTab().catch(() => null);
    let tab;
    if (current) {
      tab = await chrome.tabs.create({ url, active, windowId: current.windowId });
      if (active) await chrome.windows.update(current.windowId, { focused: true }).catch(() => {});
    } else {
      const win = await chrome.windows.create({ url, focused: true });
      tab = win.tabs[0];
    }
    return { tab: info(tab) };
  },
  "tabs.navigate": async ({ tabId, url, active = true }) => {
    const tab = await chrome.tabs.update(tabId, { url, active });
    if (active) await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
    return { tab: info(tab) };
  },
  "tabs.close": async ({ tabId }) => {
    await chrome.tabs.remove(tabId);
    return {};
  },
  "tabs.history": async (args) => {
    const tab = await tabFor(args);
    const nav = navigation(tab.id, NAV_WAIT_MS);
    try {
      if (args.op === "back") await chrome.tabs.goBack(tab.id);
      else if (args.op === "forward") await chrome.tabs.goForward(tab.id);
      else await chrome.tabs.reload(tab.id);
    } catch (e) {
      nav.cancel();
      throw new Error(args.op === "back" ? "nothing to go back to" : args.op === "forward" ? "nothing to go forward to" : String(e.message || e));
    }
    const effect = await nav.promise;
    return { effect, tab: info(await chrome.tabs.get(tab.id)) };
  },
  "tabs.wait": async ({ tabId, status, urlPrefix, timeout }) => waitTab(tabId, { status, urlPrefix, timeout }),

  // --- pages ---
  "page.map": async (args) => {
    const tab = await tabFor(args);
    const started = performance.now();
    const results = await inFrames(tab.id, args.frameIds || null, "map", [{ words: args.words || [], sections: !!args.sections, limit: args.limit || 300 }]);
    const frames = results.filter((r) => r.result && Array.isArray(r.result.elements)).sort((a, b) => a.frameId - b.frameId);
    const elements = [];
    let top = null;
    const limit = args.limit || 300;
    for (const r of frames) {
      if (r.frameId === 0) top = r.result;
      for (const e of r.result.elements) {
        if (elements.length >= limit) break;
        elements.push({ ...e, id: `${r.frameId}:${e.id}`, frame: r.frameId });
      }
    }
    return {
      tab: info(tab), url: top ? top.url : tab.url, title: top ? top.title : tab.title,
      viewport: top ? top.viewport : null, scroll: top ? top.scroll : null, elements,
      total: frames.reduce((n, r) => n + (r.result.total || 0), 0), frames: frames.length,
      frameMs: frames.map((r) => r.result.ms), ms: Math.round(performance.now() - started),
    };
  },
  "page.click": async (args) => {
    const tab = await tabFor(args);
    const [frameId, n] = splitId(args.id);
    const nav = navigation(tab.id, (args.highlightMs || 300) + NAV_WAIT_MS);
    const clicked = inFrames(tab.id, [frameId], "click", [n, { label: args.label || "", highlightMs: args.highlightMs ?? 300 }])
      .then((rs) => (rs[0] ? rs[0].result : { ok: false, error: "frame gone" }), (e) => ({ ok: false, error: String(e.message || e), lost: true }));
    const first = await Promise.race([clicked.then((r) => ({ from: "page", r })), nav.promise.then((v) => ({ from: "tab", v }))]);
    if (first.from === "tab" && first.v === "navigated") return { ok: true, effect: "navigated", tab: info(await chrome.tabs.get(tab.id)) };
    const r = first.from === "page" ? first.r : await clicked;
    if (r && r.ok && r.effect === "none") {
      // nothing changed in the page yet: a link may still be starting its navigation (give it a moment)
      const late = await Promise.race([nav.promise, new Promise((resolve) => setTimeout(() => resolve(null), LATE_NAV_MS))]);
      if (late === "navigated") r.effect = "navigated";
    }
    nav.cancel();
    if (r && r.lost) {
      const t = await chrome.tabs.get(tab.id);
      if (t.status === "loading" || t.url !== tab.url) return { ok: true, effect: "navigated", tab: info(t) };
    }
    return r;
  },
  "page.call": async (args) => {
    // fill / read / highlight / scroll / scrollTo / wait / describe in one frame (default: the top one)
    const tab = await tabFor(args);
    const frameId = args.id !== undefined ? splitId(args.id)[0] : args.frameId || 0;
    const fnArgs = (args.args || []).map((a) => (a === "$n" ? splitId(args.id)[1] : a));
    const [r] = await inFrames(tab.id, [frameId], args.fn, fnArgs);
    return r ? r.result : { ok: false, error: "frame gone" };
  },

  // --- Canvas (SPEC §10): Canvas's own JSON, fetched from inside the logged-in tab (same origin, his cookies) ---
  "canvas.fetch": async ({ tabId, path }) => {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      func: async (p) => {
        try {
          const res = await fetch(p, { credentials: "same-origin", headers: { Accept: "application/json" } });
          const text = await res.text();
          return { status: res.status, url: res.url, type: res.headers.get("content-type") || "", link: res.headers.get("link") || "", text };
        } catch (e) {
          return { status: 0, error: String(e) };
        }
      },
      args: [path],
    });
    return r ? r.result : { status: 0, error: "frame gone" };
  },
  "canvas.dom": async ({ tabId }) => {
    const [r] = await inFrames(tabId, [0], "canvasItems", []);
    return r ? r.result : { items: [] };
  },
};
