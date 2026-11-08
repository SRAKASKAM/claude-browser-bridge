// The pairing token lives in chrome.storage.local, pasted into the popup during
// setup. Deliberately NOT a generated file next to this one: that file is wiped
// every time the plugin updates, and a secret sitting in a repo checkout is a
// secret that eventually gets committed.

const DEFAULT_PORT = 8799;
const CDP_VERSION = "1.3";

let socket = null;
let backoffMs = 500;
let profile = null; // this Chrome profile's label, set from the popup
let token = null; // pairing token shared with the local server
let port = DEFAULT_PORT;
const attached = new Set(); // tabIds we hold a debugger session on

// ------------------------------------------------------------------ identity

/** Which OS Chrome runs on — the select-all keystroke differs per platform. */
let platformOs = null;
async function osName() {
  if (platformOs === null) {
    try {
      platformOs = (await chrome.runtime.getPlatformInfo()).os;
    } catch {
      platformOs = "unknown";
    }
  }
  return platformOs;
}

/** Pairing token and port, as stored by the popup. */
async function loadToken() {
  const cfg = await chrome.storage.local.get(["token", "port"]);
  token = cfg.token || null;
  port = cfg.port || DEFAULT_PORT;
  return token;
}

/** The label the agent addresses this browser by.
 *
 * An explicit label from the popup always wins. Failing that, derive one from
 * the profile's signed-in account: one extension folder is loaded into every
 * Chrome profile, so without a self-derived name they all announce themselves
 * identically and collide on the server. Self-labelling removes a manual step
 * that has to be repeated per profile and is easy to forget.
 */
async function loadProfile() {
  const { profile: stored } = await chrome.storage.local.get("profile");
  if (stored) {
    profile = stored;
    return profile;
  }
  try {
    const info = await chrome.identity.getProfileUserInfo({ accountStatus: "ANY" });
    if (info && info.email) {
      profile = info.email;
      return profile;
    }
  } catch {
    // identity unavailable (profile not signed in) — fall through to unnamed
  }
  profile = null;
  return profile;
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.op === "status") {
    reply({
      profile,
      port,
      hasToken: !!token,
      connected: !!socket && socket.readyState === WebSocket.OPEN,
    });
    return true;
  }
  if (msg.op === "setToken") {
    chrome.storage.local
      .set({ token: msg.token, port: msg.port || DEFAULT_PORT })
      .then(() => {
        token = msg.token;
        port = msg.port || DEFAULT_PORT;
        // A new token means the old socket is authenticated against nothing.
        if (socket) socket.close();
        socket = null;
        connect();
        reply({ ok: true });
      });
    return true;
  }
  if (msg.op === "setProfile") {
    chrome.storage.local.set({ profile: msg.profile }).then(() => {
      profile = msg.profile;
      // Relabel in place if we are already up; otherwise the next hello carries it.
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ op: "relabel", profile }));
      } else {
        connect();
      }
      reply({ profile, connected: !!socket && socket.readyState === WebSocket.OPEN });
    });
    return true;
  }
  return false;
});

// ---------------------------------------------------------------- transport

// Guards a race introduced by connect() being async: the `await` below is a
// suspension point, so two callers (the 15s ticker and the keepalive alarm) can
// both pass the readyState check, and the FIRST socket's onopen then fires
// against the SECOND socket — "InvalidStateError: send ... Still in CONNECTING".
// Hence both a re-entry flag and, more importantly, handlers that close over
// their own socket (`ws`) instead of reading the module-level `socket`.
let connecting = false;

async function connect() {
  if (connecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  connecting = true;

  try {
    if (profile === null) await loadProfile();
    if (token === null) await loadToken();
    if (!token) {
      // Not paired yet. Stay quiet rather than hammering a socket that would be
      // rejected anyway — the popup calls connect() the moment a token lands.
      return;
    }

    const ws = new WebSocket(`ws://127.0.0.1:${port}/ext`);
    socket = ws;

    ws.onopen = () => {
      backoffMs = 500;
      ws.send(JSON.stringify({ token, profile, userAgent: navigator.userAgent }));
      console.log(`[bridge] connected as ${profile || "unnamed"}`);
    };

    ws.onmessage = async (event) => {
      const msg = JSON.parse(event.data);
      if (msg.op === "pong") return;
      if (msg.fatal) {
        console.error("[bridge] server rejected us:", msg.fatal);
        return;
      }
      try {
        const result = await dispatch(msg.op, msg.args || {});
        ws.send(JSON.stringify({ id: msg.id, ok: true, result }));
      } catch (err) {
        ws.send(JSON.stringify({ id: msg.id, ok: false, error: String(err && err.message || err) }));
      }
    };

    ws.onclose = () => {
      if (socket === ws) socket = null;
      backoffMs = Math.min(backoffMs * 2, 10000);
      setTimeout(connect, backoffMs);
    };

    ws.onerror = () => { /* onclose handles the retry */ };
  } finally {
    // Safe to clear here: `socket` is already assigned, so the readyState guard
    // above now blocks re-entry for as long as it is CONNECTING.
    connecting = false;
  }
}

// An MV3 service worker is evicted when idle. The alarm wakes it back up and
// socket traffic keeps it alive while it runs.
chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
setInterval(() => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ op: "ping" }));
  else connect();
}, 15000);
connect();

// ------------------------------------------------------------------ helpers

async function targetTab(args) {
  let tab;
  if (args.tabId) tab = await chrome.tabs.get(args.tabId);
  else [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab");
  // Even read-only ops want the page awake: a hidden page may never render
  // the content they are about to read.
  if (await inAgentWindow(tab)) await attach(tab.id);
  return tab;
}

/** Attach a debugger session so input events are browser-level (trusted). */
async function attach(tabId) {
  if (attached.has(tabId)) return true;
  try {
    await chrome.debugger.attach({ tabId }, CDP_VERSION);
    attached.add(tabId);
  } catch (err) {
    // Already attached by DevTools / another client, or policy-blocked.
    return false;
  }
  // The emulation lives and dies with the debugger session, so it is set on
  // every fresh attach. Only in the agent window: in the user's own window it
  // would keep his background tabs running as if he were looking at them.
  try {
    if (await inAgentWindow(await chrome.tabs.get(tabId))) {
      await cdp(tabId, "Emulation.setFocusEmulationEnabled", { enabled: true });
    }
  } catch {
    // tab closed or detached in between — the attach itself still stands
  }
  return true;
}

function cdp(tabId, method, params = {}) {
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

chrome.debugger.onDetach.addListener(({ tabId }) => attached.delete(tabId));
chrome.tabs.onRemoved.addListener((tabId) => attached.delete(tabId));

/** Run a function in the page's main world and return its value. */
async function inPage(tabId, func, args = []) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func,
    args,
  });
  return result;
}

function waitForLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve({ timedOut: true });
    }, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve({ timedOut: false });
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/** Viewport-relative click point for a selector, scrolling it into view first. */
function rectOf(selector) {
  const el = document.querySelector(selector);
  if (!el) return null;
  el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return { hidden: true };
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
}

// ------------------------------------------------------------- agent window
//
// Every agent on this profile works in ONE window that stays minimized in the
// Dock, each agent in its own tab. A window per agent, created focused, is what
// kept throwing a full-screen Chrome over whatever app the user was in.
//
// A minimized window's pages are "hidden": no animation frames, and wheel
// events are never acked. Focus emulation over the debugger session makes the
// page visible and focused again — rendering, wheel, shadow-DOM dropdowns and
// coordinate clicks all work while the window sits in the Dock.

// The agent window is the one holding a pinned about:blank#claude-agents tab.
// Nothing is stored: the marker travels with its window, so the window is found
// again after a restart under its new id, and it outlives the agent tabs
// closing — it is created once, not once per task. Not an extension page:
// reloading the extension closes those, which would orphan the window.
const AGENT_PAGE = "about:blank#claude-agents";

async function agentWindowId() {
  const tabs = await chrome.tabs.query({});
  // Right after the window is created the tab may only have a pendingUrl.
  const marker = tabs.find((t) => (t.url || t.pendingUrl) === AGENT_PAGE);
  return marker ? marker.windowId : null;
}

// Tabs where the user has been asked to act; the window must not be minimized
// under him while any is outstanding — he may have switched app to fetch a code.
async function attentionTabs() {
  const { attentionTabs: ids } = await chrome.storage.local.get("attentionTabs");
  return ids || [];
}

// Serialized: two agents (or the user closing tabs) must not lose each other's entry.
let attentionLock = Promise.resolve();

function setAttention(tabId, on) {
  const run = attentionLock.then(async () => {
    const ids = await attentionTabs();
    if (!on && !ids.includes(tabId)) return;
    await chrome.storage.local.set({ attentionTabs: [...ids.filter((id) => id !== tabId), ...(on ? [tabId] : [])] });
  });
  attentionLock = run.catch(() => {});
  return run;
}

chrome.tabs.onRemoved.addListener((tabId) => setAttention(tabId, false));

/** Minimize the agent window unless the user is in it or has a step open in it. */
async function minimizeAgentWindow(windowId) {
  const win = await chrome.windows.get(windowId, { populate: true });
  if (win.state === "minimized") return { hidden: true };
  if (win.focused) return { hidden: false, reason: "the user is in it" };
  const asked = await attentionTabs();
  if (win.tabs.some((t) => asked.includes(t.id))) return { hidden: false, reason: "a human step is open in it" };
  await chrome.windows.update(windowId, { state: "minimized" });
  return { hidden: true };
}

// A restored session brings the agent window back in whatever state it was.
chrome.runtime.onStartup.addListener(async () => {
  await chrome.storage.local.remove("attentionTabs");
  const id = await agentWindowId();
  if (id !== null) await minimizeAgentWindow(id);
});

// Two agents calling openTab at once must not both create a window.
let windowLock = Promise.resolve();

async function ensureAgentWindow() {
  const run = windowLock.then(async () => {
    const existing = await agentWindowId();
    if (existing !== null) {
      // Left open after a human step and the user has moved on: back to the Dock.
      await minimizeAgentWindow(existing);
      return existing;
    }
    // `state: "minimized"` on create is ignored on macOS — the window opens
    // maximized. Create it unfocused and minimize it straight away: the only
    // moment it is ever on screen, once per profile per browser start.
    const win = await chrome.windows.create({ url: AGENT_PAGE, focused: false, width: 1440, height: 900 });
    await chrome.windows.update(win.id, { state: "minimized" });
    await chrome.tabs.update(win.tabs[0].id, { pinned: true });
    return win.id;
  });
  windowLock = run.catch(() => {});
  return run;
}

async function inAgentWindow(tab) {
  return tab.windowId === (await agentWindowId());
}


// --------------------------------------------------------------- operations

async function dispatch(op, args) {
  const ops = {
    tabs: async () => {
      const tabs = await chrome.tabs.query({});
      return {
        tabs: tabs.map((t) => ({ tabId: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId })),
      };
    },

    // Switches the tab inside its own window. Never raises the window: that is
    // `reveal`, and only a human click on the banner should ever call it.
    activate: async () => {
      const tab = await chrome.tabs.get(args.tabId);
      await chrome.tabs.update(tab.id, { active: true });
      return { tabId: tab.id, url: tab.url };
    },

    // A tab of the caller's own in the shared, minimized agent window.
    openTab: async () => {
      const windowId = await ensureAgentWindow();
      const tab = await chrome.tabs.create({ windowId, url: args.url || "about:blank", active: false });
      await attach(tab.id);
      const load = args.url ? await waitForLoad(tab.id, args.timeoutMs || 30000) : {};
      const fresh = await chrome.tabs.get(tab.id);
      return { tabId: fresh.id, windowId, url: fresh.url, title: fresh.title, ...load };
    },

    // Kept for agents still following the old instructions: it used to open a
    // focused window of its own. It now opens a tab in the agent window, and
    // ignores `focused`, `width` and `height`.
    newWindow: async () => await ops.openTab(),

    // Closes the caller's tab. Only tabs in the agent window — the user's tabs
    // are never an agent's to close.
    closeTab: async () => {
      const tab = await chrome.tabs.get(args.tabId);
      if (!(await inAgentWindow(tab))) throw new Error("closeTab only closes tabs in the agent window");
      if (tab.url === AGENT_PAGE) throw new Error("that is the agent window's own tab, not yours");
      await chrome.tabs.remove(tab.id);
      return { closed: tab.id };
    },

    // Old counterpart of newWindow: closes only the caller's tab (bb passes the
    // pinned tabId) — other agents work in the agent window too. Agents no
    // longer own whole windows, so any other window is the user's: refused.
    closeWindow: async () => {
      if (args.windowId !== (await agentWindowId())) throw new Error("agents no longer own windows — close your tab with `bb close`");
      if (typeof args.tabId !== "number") throw new Error("the agent window is shared — close your own tab with `bb close`");
      return await ops.closeTab();
    },

    // Bring a tab in front of the user — for the banner's click handler, when
    // he has asked to see it. Agents do not call this to get his attention.
    reveal: async () => {
      const tab = await chrome.tabs.get(args.tabId);
      await chrome.tabs.update(tab.id, { active: true });
      await chrome.windows.update(tab.windowId, { state: "normal", focused: true });
      return { tabId: tab.id, windowId: tab.windowId };
    },

    // Send the agent window back to the Dock, unless the user is in it or a
    // human step is still open there.
    hide: async () => {
      const id = await agentWindowId();
      if (id === null) return { hidden: false };
      return await minimizeAgentWindow(id);
    },

    // Mark what the user has to do himself: a red outline on each selector and
    // a red bar on top of the page with the message.
    attention: async () => {
      const tab = await targetTab(args);
      await ops.clearAttention();
      await setAttention(tab.id, true);
      const found = await inPage(
        tab.id,
        (selectors, message) => {
          const hits = [];
          selectors.forEach((sel) => {
            let el = null;
            try {
              el = document.querySelector(sel);
            } catch {
              // a malformed selector must not cost the other outlines and the bar
            }
            if (!el) return;
            hits.push(sel);
            if (el.dataset.claudeAttention !== undefined) return; // two selectors, one element
            el.dataset.claudeAttention = JSON.stringify([el.style.outline, el.style.outlineOffset]);
            el.style.outline = "4px solid #E11D48";
            el.style.outlineOffset = "3px";
            if (hits.length === 1) el.scrollIntoView({ block: "center", behavior: "instant" });
          });
          if (message) {
            const bar = document.createElement("div");
            bar.id = "claude-attention-bar";
            bar.dataset.claudeAttention = "[]";
            bar.textContent = message;
            bar.style.cssText =
              "position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 16px;" +
              "background:#E11D48;color:#fff;font:600 15px/1.3 -apple-system,system-ui,sans-serif;" +
              "text-align:center;pointer-events:none";
            document.documentElement.appendChild(bar);
          }
          return hits;
        },
        [args.selectors || [], args.message || ""],
      );
      return { tabId: tab.id, outlined: found, missing: (args.selectors || []).filter((s) => !found.includes(s)) };
    },

    clearAttention: async () => {
      const tab = await targetTab(args);
      await setAttention(tab.id, false);
      await inPage(tab.id, () => {
        document.querySelectorAll("[data-claude-attention]").forEach((el) => {
          if (el.id === "claude-attention-bar") return el.remove();
          [el.style.outline, el.style.outlineOffset] = JSON.parse(el.dataset.claudeAttention);
          delete el.dataset.claudeAttention;
        });
      });
      return { cleared: tab.id };
    },

    navigate: async () => {
      // A new tab goes into the agent window, never the user's.
      if (args.newTab) return await ops.openTab();
      let tab = await targetTab(args);
      await chrome.tabs.update(tab.id, { url: args.url });
      const load = await waitForLoad(tab.id, args.timeoutMs || 30000);
      tab = await chrome.tabs.get(tab.id);
      return { tabId: tab.id, url: tab.url, title: tab.title, ...load };
    },

    click: async () => {
      const tab = await targetTab(args);
      const rect = await inPage(tab.id, rectOf, [args.selector]);
      if (!rect) throw new Error(`selector not found: ${args.selector}`);
      if (rect.hidden) throw new Error(`element has zero size: ${args.selector}`);
      if (await attach(tab.id)) {
        const base = { x: Math.round(rect.x), y: Math.round(rect.y), button: "left", clickCount: 1 };
        await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mouseMoved", clickCount: 0 });
        await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mousePressed" });
        await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mouseReleased" });
        return { clicked: args.selector, trusted: true, at: base };
      }
      await inPage(tab.id, (sel) => document.querySelector(sel).click(), [args.selector]);
      return { clicked: args.selector, trusted: false };
    },

    // Click by viewport coordinates — for cross-origin widgets (captcha
    // iframes) where we cannot query a selector.
    clickAt: async () => {
      const tab = await targetTab(args);
      if (!(await attach(tab.id))) throw new Error("need a debugger session for coordinate clicks");
      const base = { x: Math.round(args.x), y: Math.round(args.y), button: "left", clickCount: 1 };
      await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mouseMoved", clickCount: 0 });
      await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mousePressed" });
      await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mouseReleased" });
      return { clickedAt: base, trusted: true };
    },

    // Canvas apps (Miro, maps, Figma) pan on a plain wheel and zoom on
    // Ctrl+wheel — the same event a trackpad pinch produces.
    wheel: async () => {
      const tab = await targetTab(args);
      if (!(await attach(tab.id))) throw new Error("need a debugger session for wheel events");
      const base = { x: Math.round(args.x), y: Math.round(args.y) };
      await cdp(tab.id, "Input.dispatchMouseEvent", { ...base, type: "mouseMoved" });
      // Chrome acks a wheel event only after the compositor handles it, and a
      // tab in an occluded window may never produce that frame — don't hang on it.
      await Promise.race([
        cdp(tab.id, "Input.dispatchMouseEvent", {
          ...base,
          type: "mouseWheel",
          deltaX: args.deltaX || 0,
          deltaY: args.deltaY || 0,
          modifiers: args.ctrl ? 2 : 0, // 2 = Ctrl
        }),
        new Promise((resolve) => setTimeout(resolve, 1500)),
      ]);
      return { wheelAt: base, deltaX: args.deltaX || 0, deltaY: args.deltaY || 0, ctrl: !!args.ctrl };
    },

    type: async () => {
      const tab = await targetTab(args);
      if (args.selector) await ops.click();
      if (await attach(tab.id)) {
        if (args.clear) {
          // Select-all is not one keystroke across platforms, and getting it
          // wrong fails SILENTLY: nothing is selected, so the new text is
          // appended to the old value instead of replacing it.
          //
          // macOS: a plain Cmd+A key event does not select anything — the
          // editing command has to be named, because Chrome routes it through
          // the NSResponder key bindings rather than the page.
          // Windows/Linux: `commands` is a macOS-only concept and is ignored;
          // there, real Ctrl+A key events are what select the field.
          const mac = (await osName()) === "mac";
          const mods = mac ? 4 : 2; // 4 = Meta, 2 = Ctrl
          const base = { key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: mods };
          await cdp(tab.id, "Input.dispatchKeyEvent", {
            type: "rawKeyDown",
            ...base,
            ...(mac ? { commands: ["selectAll"] } : {}),
          });
          await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
        }
        await cdp(tab.id, "Input.insertText", { text: args.text });
        return { typed: args.text.length, trusted: true, platform: await osName() };
      }
      await inPage(
        tab.id,
        (sel, text) => {
          const el = sel ? document.querySelector(sel) : document.activeElement;
          el.focus();
          el.value = text;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
        },
        [args.selector || null, args.text],
      );
      return { typed: args.text.length, trusted: false };
    },

    key: async () => {
      const tab = await targetTab(args);
      if (!(await attach(tab.id))) throw new Error("need a debugger session to send keys");
      const map = {
        Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" },
        Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
        Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
        Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
        ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
        ArrowUp: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
      };
      const spec = map[args.key];
      if (!spec) throw new Error(`unsupported key: ${args.key}`);
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyDown", ...spec });
      await cdp(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", ...spec });
      return { key: args.key };
    },

    // Runtime.evaluate goes through the debugger, so page CSP does not block it.
    eval: async () => {
      const tab = await targetTab(args);
      if (await attach(tab.id)) {
        const res = await cdp(tab.id, "Runtime.evaluate", {
          expression: args.expression,
          returnByValue: true,
          awaitPromise: true,
        });
        if (res.exceptionDetails) throw new Error(res.exceptionDetails.text || "eval threw");
        return { value: res.result.value };
      }
      const value = await inPage(tab.id, (code) => eval(code), [args.expression]);
      return { value, viaScripting: true };
    },

    text: async () => {
      const tab = await targetTab(args);
      const limit = args.limit || 20000;
      const out = await inPage(
        tab.id,
        (sel, lim) => {
          const root = sel ? document.querySelector(sel) : document.body;
          if (!root) return null;
          return (root.innerText || "").slice(0, lim);
        },
        [args.selector || null, limit],
      );
      if (out === null) throw new Error(`selector not found: ${args.selector}`);
      return { url: tab.url, title: tab.title, text: out };
    },

    // Compact list of things worth clicking, with usable selectors.
    snapshot: async () => {
      const tab = await targetTab(args);
      const items = await inPage(tab.id, (max) => {
        const sel = "a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[contenteditable=true]";
        const path = (el) => {
          if (el.id) return `#${CSS.escape(el.id)}`;
          if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
          const parts = [];
          let node = el;
          while (node && node.nodeType === 1 && parts.length < 5) {
            const tag = node.tagName.toLowerCase();
            if (tag === "html" || tag === "body") break;
            const siblings = [...(node.parentNode?.children || [])].filter((s) => s.tagName === node.tagName);
            parts.unshift(siblings.length > 1 ? `${tag}:nth-of-type(${siblings.indexOf(node) + 1})` : tag);
            node = node.parentNode;
          }
          return parts.join(" > ");
        };
        const out = [];
        for (const el of document.querySelectorAll(sel)) {
          const r = el.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue;
          out.push({
            tag: el.tagName.toLowerCase(),
            type: el.type || undefined,
            text: (el.innerText || el.value || el.placeholder || el.getAttribute("aria-label") || "").trim().slice(0, 80),
            selector: path(el),
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
          });
          if (out.length >= max) break;
        }
        const frames = [...document.querySelectorAll("iframe")].map((f) => {
          const r = f.getBoundingClientRect();
          return { src: (f.src || "").slice(0, 120), x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
        });
        return { elements: out, iframes: frames };
      }, [args.max || 120]);
      return { url: tab.url, title: tab.title, ...items };
    },

    shot: async () => {
      const tab = await targetTab(args);
      // captureVisibleTab only ever photographs the FOREGROUND tab, so using it
      // means activating this one first — which yanks the window away from
      // whoever was looking at it, the user or a second agent. CDP screenshots
      // a background tab in place, so prefer it and keep the fallback for tabs
      // the debugger cannot attach to (DevTools open, policy-blocked).
      if (await attach(tab.id)) {
        try {
          const { data } = await cdp(tab.id, "Page.captureScreenshot", { format: "png" });
          return { url: tab.url, title: tab.title, dataUrl: `data:image/png;base64,${data}` };
        } catch {
          // Attached but the command failed (a page CDP will not paint, a
          // detach mid-flight). Fall through rather than losing the screenshot.
        }
      }
      // The fallback switches the window's foreground tab, which is fine in
      // the agent window and not in the user's.
      if (!(await inAgentWindow(tab))) throw new Error("screenshot needs a debugger session on this tab (DevTools open on it?)");
      await chrome.tabs.update(tab.id, { active: true });
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      return { url: tab.url, title: tab.title, dataUrl, activated: true };
    },

    waitFor: async () => {
      const tab = await targetTab(args);
      const deadline = Date.now() + (args.timeoutMs || 20000);
      while (Date.now() < deadline) {
        const hit = await inPage(
          tab.id,
          (sel, needle) => {
            if (sel) return !!document.querySelector(sel);
            return (document.body.innerText || "").includes(needle);
          },
          [args.selector || null, args.text || ""],
        );
        if (hit) return { found: true, waitedMs: Date.now() - (deadline - (args.timeoutMs || 20000)) };
        await new Promise((r) => setTimeout(r, 400));
      }
      return { found: false };
    },

    // Confirms which logged-in identity these commands are landing in, before
    // anything is typed into it.
    whoami: async () => {
      const tabs = await chrome.tabs.query({});
      const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      return {
        profile,
        tabCount: tabs.length,
        activeTab: active ? { tabId: active.id, url: active.url, title: active.title } : null,
      };
    },

    // Lets edits to this file take effect without touching chrome://extensions,
    // which no script is allowed to drive.
    reloadSelf: async () => {
      setTimeout(() => chrome.runtime.reload(), 200);
      return { reloading: true };
    },

    detach: async () => {
      // Not targetTab: in the agent window that would attach first.
      const tab = await chrome.tabs.get(args.tabId);
      if (attached.has(tab.id)) {
        await chrome.debugger.detach({ tabId: tab.id });
        attached.delete(tab.id);
      }
      return { detached: tab.id };
    },
  };

  const fn = ops[op];
  if (!fn) throw new Error(`unknown op: ${op}`);
  return await fn();
}
