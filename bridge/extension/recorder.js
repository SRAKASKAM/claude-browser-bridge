// Records a browser session in the user's own tab: console, exceptions,
// network (with bodies), the user's steps, a screenshot per step and rrweb
// DOM history — streamed out as it happens, for the host to redact and store.
//
// Self-contained on purpose: it knows nothing about the bridge's socket, its
// agent window or its ops. A host wires it up with `createRecorder(host)`:
//
//   host.send(json)            deliver one serialized event; return false if it
//                              could not (the recorder queues and `flush()`es later)
//   host.cdp(tabId, m, p)      chrome.debugger.sendCommand for that tab
//   host.attach(tabId)         make sure a debugger session exists; true on success
//   host.isAttached(tabId)     whether the host already holds a session on it
//   host.release(tabId)        detach a session the recorder attached itself
//   host.excludedWindowId()    a window never to pick a tab from (or null)
//
// and gets back `{ start, stop, status, isRecording, flush }`:
//
//   start({ rec, tabId? })     record tabId, or the user's active tab; rec is the
//                              caller's id for this recording
//   stop({ rec? })             stop, after pending bodies and screenshots land
//   status()                   { recording: false } | { recording: true, rec, tabs, steps }
//
// Every event goes out as {op: "rec", rec, t, tabId, method, params}: CDP events
// verbatim (Runtime.consoleAPICalled, Runtime.exceptionThrown, Log.entryAdded,
// Network.*), plus rec.start, rec.stop, rec.action {seq, type, selector, text,
// value, url, rrwebId}, rec.shot {seq, data}, rec.body, rec.postData,
// rec.perf {t, duration, blocking, scripts} (a frame of 100 ms+),
// rec.rrweb {event}, rec.tab, rec.detached. bridge/recording.py turns that
// stream into the bundle and documents its layout.
//
// Nothing is buffered beyond a dropped transport — a service worker can be
// evicted, and a recording that lived in its memory would go with it.
//
// One debugger session per tab, shared with the host: if the tab was already
// attached, recording reuses that session and leaves it attached on stop;
// otherwise it releases what it attached.
//
// Host requirements: a module service worker; the permissions "debugger",
// "webNavigation", "tabs" and "storage" (chrome.storage.session); host access
// to the recorded pages; and rrweb-record.min.js (@rrweb/record 2.1.7,
// umd/record.min.js) at the extension root.

// Response bodies are sent for these resource types only (keep in sync with
// BODY_TYPES in bridge/recording.py).
const BODY_TYPES = new Set(["XHR", "Fetch", "Document"]);
// Bodies go out whole, up to this many characters (UTF-16, or base64 for binary
// ones): the host must redact a body before it cuts it — a cut JSON body no
// longer parses, and its secrets would slip through. The host cuts what it keeps.
const SEND_CAP = 8 * 1024 * 1024;
const BINDING = "__claudeRec";
// Console and log entries Chrome replays on enable, from before the recording.
const SLACK_MS = 1000;
const FORWARDED = new Set([
  "Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded",
  "Network.requestWillBeSent", "Network.requestWillBeSentExtraInfo", "Network.responseReceived",
  "Network.responseReceivedExtraInfo", "Network.loadingFinished", "Network.loadingFailed",
  "Network.webSocketCreated", "Network.webSocketFrameSent", "Network.webSocketFrameReceived",
  "Network.webSocketFrameError",
]);
const STATE_KEY = "claudeRec.state"; // chrome.storage.session

/** Runs in the page's main world: rrweb plus the user's steps, both out through the binding.
 *
 * Secret fields are masked here, inside the page, so their values never reach
 * the extension at all: passwords, one-time codes, card fields, hidden inputs.
 */
function recorderPage(rrwebFactory, binding) {
  if (window.top !== window || window.__claudeRecOn) return;
  window.__claudeRecOn = true;
  const send = (obj) => {
    try {
      window[binding](JSON.stringify(obj));
    } catch {
      // binding removed (recording stopped) or an unserializable value
    }
  };
  const holder = {};
  rrwebFactory.call(holder);
  const rr = holder.rrwebRecord;

  const SECRET = /pass|otp|one-time|cvc|cvv|card|cc-|\bpin\b|secret|token|code/i;
  const isSecret = (el) => {
    if (!el || !el.getAttribute) return false;
    const type = (el.type || "").toLowerCase();
    if (type === "password" || type === "hidden") return true;
    const hay = ["name", "id", "autocomplete", "aria-label", "placeholder"].map((n) => el.getAttribute(n) || "").join(" ");
    return SECRET.test(hay);
  };
  const mask = (text, el) => (isSecret(el) ? "*".repeat((text || "").length) : text);
  const types = ["color", "date", "datetime-local", "email", "month", "number", "range", "search", "tel",
    "text", "time", "url", "week", "textarea", "select", "password", "hidden"];

  const stop = rr.record({
    emit: (event) => send({ k: "rr", event }),
    // Every input type goes through maskInputFn, which masks only secret ones.
    maskInputOptions: Object.fromEntries(types.map((t) => [t, true])),
    maskInputFn: mask,
    sampling: { mousemove: false, scroll: 250, input: "last" },
    recordAfter: "DOMContentLoaded",
    recordCanvas: false,
    collectFonts: false,
  });

  // The same selector shape as the bridge's snapshot op, so an agent can reuse it.
  const selectorOf = (el) => {
    if (!el || el.nodeType !== 1) return "";
    // Attributes, not properties: on a <form>, an input named "id" shadows form.id.
    const id = el.getAttribute("id");
    const name = el.getAttribute("name");
    if (id) return `#${CSS.escape(id)}`;
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
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
  // Never an input's value: that is what `value` is for, masked.
  const label = (el) => {
    const field = /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName || "");
    const attr = (n) => el.getAttribute(n);
    const text = (!field && el.innerText) || attr("aria-label") || attr("placeholder") || attr("title") || attr("alt") || attr("name") || "";
    return String(text).replace(/\s+/g, " ").trim().slice(0, 120);
  };
  const act = (type, el, extra = {}) => {
    if (!el || el.nodeType !== 1) return;
    send({ k: "act", type, selector: selectorOf(el), text: label(el), tag: el.tagName.toLowerCase(),
      url: location.href, rrwebId: rr.record.mirror.getId(el), ...extra });
  };
  const target = (ev) => ev.composedPath()[0];
  const listeners = [
    ["click", (ev) => {
      const el = target(ev);
      const hit = (el.closest && el.closest("a,button,input,select,textarea,label,summary,[role],[onclick],[tabindex]")) || el;
      act("click", hit, { x: Math.round(ev.clientX), y: Math.round(ev.clientY) });
    }],
    ["change", (ev) => {
      const el = target(ev);
      const value = el.type === "checkbox" || el.type === "radio" ? String(el.checked) : String(el.value ?? "");
      act("input", el, { value: mask(value, el) });
    }],
    ["submit", (ev) => act("submit", target(ev))],
    ["keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === "Escape") act("key", target(ev), { key: ev.key });
    }],
  ];
  listeners.forEach(([name, fn]) => addEventListener(name, fn, true));

  // Frames of 100 ms+ and the scripts that ran in them: why a click froze the page.
  let frames = null;
  try {
    frames = new PerformanceObserver((list) => {
      for (const f of list.getEntries()) {
        if (f.duration < 100) continue;
        const scripts = [...(f.scripts || [])].sort((a, b) => b.duration - a.duration).slice(0, 5).map((s) => ({
          duration: Math.round(s.duration), invoker: s.invoker, function: s.sourceFunctionName,
          url: s.sourceURL, char: s.sourceCharPosition, forcedLayout: Math.round(s.forcedStyleAndLayoutDuration || 0),
        }));
        send({ k: "perf", t: Math.round(performance.timeOrigin + f.startTime), duration: Math.round(f.duration),
          blocking: Math.round(f.blockingDuration || 0), render: Math.round(f.renderStart ? f.startTime + f.duration - f.renderStart : 0),
          scripts });
      }
    });
    frames.observe({ type: "long-animation-frame" });
  } catch {
    // no Long Animation Frames API in this browser
  }

  // Dots over secret fields that are not password fields already (OTP, card,
  // "show password"), only while a screenshot is taken. Not inside iframes.
  // Screenshots overlap: only the first mask and the last unmask touch the page.
  let masks = 0;
  window.__claudeRecMask = (on) => {
    masks = Math.max(0, masks + (on ? 1 : -1));
    if (on ? masks !== 1 : masks !== 0) return;
    document.querySelectorAll("input, textarea").forEach((el) => {
      if (!isSecret(el) || el.type === "password" || el.type === "hidden") return;
      if (on) {
        el.dataset.claudeRecMasked = el.style.webkitTextSecurity || "";
        el.style.webkitTextSecurity = "disc";
      } else if (el.dataset.claudeRecMasked !== undefined) {
        el.style.webkitTextSecurity = el.dataset.claudeRecMasked;
        delete el.dataset.claudeRecMasked;
      }
    });
  };

  window.__claudeRecStop = () => {
    if (masks) {
      masks = 1;
      window.__claudeRecMask(false);
    }
    if (frames) frames.disconnect();
    if (stop) stop();
    listeners.forEach(([name, fn]) => removeEventListener(name, fn, true));
    window.__claudeRecOn = false;
  };
}

let pageSource = null;
async function recorderScript() {
  if (pageSource === null) {
    // @rrweb/record 2.1.7, umd/record.min.js from npm, unmodified (MIT).
    const rrweb = await (await fetch(chrome.runtime.getURL("rrweb-record.min.js"))).text();
    // Spliced in as code, never eval'd from a string: the page's CSP would block that.
    // `define`/`exports`/`module` are shadowed so the UMD wrapper sets rrwebRecord on
    // `this` even on pages that use AMD or CommonJS globals.
    pageSource = `(${recorderPage.toString()})(function (define, exports, module) {\n${rrweb}\n}, ${JSON.stringify(BINDING)});`;
  }
  return pageSource;
}

/** Call once, synchronously, at the top level of the service worker: it registers listeners. */
export function createRecorder(host) {
  let rec = null; // { rec, startedAt, seq, tabs: Set, own: Set, scripts: Map, types: Map, pending: Set }
  const queue = []; // serialized events the transport could not take yet
  let dropped = 0; // events lost to a full queue, reported on the next flush
  let resuming = null; // a resume() in flight, which stop() must not race

  const fresh = (id, seq = 0) => ({ rec: id, startedAt: Date.now(), seq, tabs: new Set(), own: new Set(),
    scripts: new Map(), types: new Map(), pending: new Set() });

  /** `owner`: the recording a late body or screenshot belongs to; dropped if that one has ended. */
  function emit(tabId, method, params, owner = rec) {
    if (!rec || owner !== rec) return; // a straggler after stop gave up waiting for it
    const msg = JSON.stringify({ op: "rec", rec: rec.rec, t: Date.now(), tabId, method, params });
    if (host.send(msg)) return;
    if (queue.length < 5000) queue.push(msg);
    else dropped++;
  }

  function flush() {
    while (queue.length && host.send(queue[0])) queue.shift();
    if (dropped && !queue.length && rec) {
      const n = dropped;
      dropped = 0;
      emit(null, "rec.dropped", { event: `${n} events`, reason: "the connection to the bridge server was down too long" });
    }
  }

  /** Remembered across a service-worker restart, so the recording can be picked up again. */
  function save() {
    return chrome.storage.session.set({ [STATE_KEY]: { rec: rec.rec, tabs: [...rec.tabs], own: [...rec.own], seq: rec.seq } });
  }

  /** Track an async fetch so stop waits for it before declaring the recording complete. */
  function track(promise) {
    const r = rec;
    const p = promise.catch(() => {}).finally(() => r.pending.delete(p));
    r.pending.add(p);
  }

  /** The tab the user is looking at: active in his last focused window, never the excluded one. */
  async function userActiveTab() {
    const excluded = await host.excludedWindowId();
    const [front] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (front && front.windowId !== excluded) return front;
    const others = (await chrome.tabs.query({ active: true, windowType: "normal" })).filter((t) => t.windowId !== excluded);
    others.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    if (!others.length) throw new Error("no tab of the user's to record — pass --tab");
    return others[0];
  }

  async function recordTab(tabId) {
    const already = host.isAttached(tabId);
    if (!(await host.attach(tabId))) throw new Error(`cannot attach the debugger to tab ${tabId} (DevTools open on it?)`);
    if (!already) rec.own.add(tabId);
    rec.tabs.add(tabId);
    await host.cdp(tabId, "Runtime.enable");
    await host.cdp(tabId, "Log.enable");
    await host.cdp(tabId, "Page.enable");
    await host.cdp(tabId, "Network.enable", { maxPostDataSize: SEND_CAP });
    await host.cdp(tabId, "Runtime.addBinding", { name: BINDING });
    const source = await recorderScript();
    const { identifier } = await host.cdp(tabId, "Page.addScriptToEvaluateOnNewDocument", { source });
    rec.scripts.set(tabId, identifier);
    // A recorder left in the page by a worker that died: restart it, so rrweb
    // takes a fresh full snapshot instead of mutating one we no longer have.
    await host.cdp(tabId, "Runtime.evaluate", { expression: "window.__claudeRecStop && window.__claudeRecStop()" });
    await host.cdp(tabId, "Runtime.evaluate", { expression: source });
  }

  async function unrecordTab(r, tabId) {
    try {
      await host.cdp(tabId, "Runtime.evaluate", { expression: "window.__claudeRecStop && window.__claudeRecStop()" });
      if (r.scripts.has(tabId)) await host.cdp(tabId, "Page.removeScriptToEvaluateOnNewDocument", { identifier: r.scripts.get(tabId) });
      await host.cdp(tabId, "Runtime.removeBinding", { name: BINDING });
      // The host's own ops need none of these enabled.
      for (const domain of ["Network", "Log", "Page", "Runtime"]) await host.cdp(tabId, `${domain}.disable`);
      if (r.own.has(tabId)) await host.release(tabId);
    } catch {
      // tab closed or already detached — nothing left to undo
    }
  }

  async function start(args) {
    if (rec) throw new Error(`a recording is already running (${rec.rec}) — stop it first`);
    if (!args.rec) throw new Error("recordStart needs a recording id");
    const tab = args.tabId ? await chrome.tabs.get(args.tabId) : await userActiveTab();
    if (!/^https?:|^file:/.test(tab.url || "")) throw new Error(`cannot record ${tab.url || "this tab"} — only web pages`);
    rec = fresh(args.rec);
    emit(tab.id, "rec.start", { url: tab.url, title: tab.title, tabId: tab.id });
    try {
      await recordTab(tab.id);
    } catch (err) {
      await unrecordTab(rec, tab.id);
      rec = null;
      throw err;
    }
    await save();
    return { rec: args.rec, tabId: tab.id, url: tab.url, title: tab.title };
  }

  async function stop(args = {}) {
    if (resuming) await resuming;
    if (!rec) await resume();
    if (!rec) throw new Error("no recording is running");
    if (args.rec && args.rec !== rec.rec) throw new Error(`recording ${args.rec} is not the running one (${rec.rec})`);
    const r = rec;
    // Bodies and screenshots still on their way need the sessions: wait first, then tear down.
    await Promise.race([Promise.allSettled([...r.pending]), new Promise((ok) => setTimeout(ok, 5000))]);
    for (const tabId of r.tabs) await unrecordTab(r, tabId);
    emit(null, "rec.stop", { steps: r.seq });
    rec = null;
    await chrome.storage.session.remove(STATE_KEY);
    return { rec: r.rec, tabs: [...r.tabs], steps: r.seq };
  }

  function status() {
    return rec ? { recording: true, rec: rec.rec, tabs: [...rec.tabs], steps: rec.seq } : { recording: false };
  }

  /** The service worker was evicted mid-recording: pick the tabs up again and mark the gap. */
  function resume() {
    resuming = resuming || doResume().finally(() => (resuming = null));
    return resuming;
  }

  async function doResume() {
    const { [STATE_KEY]: saved } = await chrome.storage.session.get(STATE_KEY);
    if (!saved || rec) return;
    rec = fresh(saved.rec, saved.seq || 0);
    const own = new Set(saved.own || []);
    for (const tabId of saved.tabs) {
      emit(tabId, "rec.detached", { reason: "the extension restarted — events in between are missing" });
      try {
        // The old session may have outlived the worker; it is ours, not DevTools'.
        if (own.has(tabId)) await host.release(tabId).catch(() => {});
        // recordTab marks the tab as ours if it had to attach it, so stop releases it.
        await recordTab(tabId);
      } catch (err) {
        emit(tabId, "rec.detached", { reason: `could not pick the tab up again: ${err.message || err}` });
      }
    }
  }

  function shot(tabId, seq, delayMs) {
    const owner = rec;
    track(new Promise((ok) => setTimeout(ok, delayMs)).then(async () => {
      // A one-time code typed into a plain text field would show in the pixels:
      // secret fields are drawn as dots for the moment of the capture.
      const mask = (on) => host.cdp(tabId, "Runtime.evaluate", { expression: `window.__claudeRecMask && window.__claudeRecMask(${on})` });
      await mask(true);
      try {
        const capture = host.cdp(tabId, "Page.captureScreenshot", { format: "jpeg", quality: 60 });
        // A tab in the background may never paint the frame.
        const { data } = await Promise.race([capture, new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 4000))]);
        emit(tabId, "rec.shot", { seq, data }, owner);
      } finally {
        await mask(false).catch(() => {});
      }
    }));
  }

  function action(tabId, step, shotDelayMs) {
    const seq = ++rec.seq;
    emit(tabId, "rec.action", { seq, ...step });
    save();
    shot(tabId, seq, shotDelayMs);
  }

  chrome.debugger.onEvent.addListener(({ tabId }, method, params) => {
    if (!rec || !rec.tabs.has(tabId)) return;
    if (method === "Runtime.bindingCalled") {
      if (params.name !== BINDING) return;
      let msg;
      try {
        msg = JSON.parse(params.payload);
      } catch {
        return;
      }
      if (msg.k === "rr") emit(tabId, "rec.rrweb", { event: msg.event });
      else if (msg.k === "perf") {
        const { k, ...frame } = msg;
        emit(tabId, "rec.perf", frame);
      }
      else if (msg.k === "act") {
        const { k, ...step } = msg;
        action(tabId, step, 400);
      }
      return;
    }
    if (method === "Page.frameNavigated" && !params.frame.parentId) {
      action(tabId, { type: "navigate", url: params.frame.url, text: "" }, 1500);
      return;
    }
    if (method === "Page.navigatedWithinDocument") {
      action(tabId, { type: "navigate", url: params.url, text: "", sameDocument: true }, 600);
      return;
    }
    if (!FORWARDED.has(method)) return;
    const stamp = method === "Log.entryAdded" ? params.entry.timestamp : params.timestamp;
    if (/consoleAPICalled|exceptionThrown|entryAdded/.test(method) && stamp < rec.startedAt - SLACK_MS) return;
    if (method === "Network.requestWillBeSent" || method === "Network.responseReceived") {
      rec.types.set(params.requestId, params.type);
    }
    if (method.startsWith("Network.webSocketFrame") && params.response?.payloadData?.length > SEND_CAP) {
      params.response.payloadData = "";
      params.response.tooLarge = true;
    }
    // The request body again, base64-encoded: the host could not redact it.
    if (method === "Network.requestWillBeSent") delete params.request.postDataEntries;
    emit(tabId, method, params);
    const owner = rec;
    if (method === "Network.requestWillBeSent" && params.request.hasPostData && params.request.postData === undefined) {
      track(host.cdp(tabId, "Network.getRequestPostData", { requestId: params.requestId }).then(({ postData = "" }) =>
        emit(tabId, "rec.postData", { requestId: params.requestId, size: postData.length,
          ...(postData.length > SEND_CAP ? { postData: "", tooLarge: true } : { postData }) }, owner)));
    }
    if (method === "Network.loadingFinished" && BODY_TYPES.has(rec.types.get(params.requestId))) {
      track(host.cdp(tabId, "Network.getResponseBody", { requestId: params.requestId }).then(({ body, base64Encoded }) =>
        emit(tabId, "rec.body", { requestId: params.requestId, base64Encoded, size: body.length,
          ...(body.length > SEND_CAP ? { body: "", tooLarge: true } : { body }) }, owner)));
    }
    if (method === "Network.loadingFinished" || method === "Network.loadingFailed") rec.types.delete(params.requestId);
  });

  // "target_closed" when the tab is closed, "canceled_by_user" when he pressed
  // Cancel on Chrome's debugging bar — that ends every session of the extension.
  chrome.debugger.onDetach.addListener(({ tabId }, reason) => {
    if (!rec || !rec.tabs.has(tabId)) return;
    emit(tabId, "rec.detached", { reason });
    rec.tabs.delete(tabId);
  });

  // Popups and "open in new tab" from a recorded tab join the recording. Not
  // tabs.onCreated: a target=_blank link is noopener and leaves openerTabId unset.
  chrome.webNavigation.onCreatedNavigationTarget.addListener(async ({ sourceTabId, tabId, url }) => {
    if (!rec || !rec.tabs.has(sourceTabId) || rec.tabs.has(tabId)) return;
    emit(tabId, "rec.tab", { tabId, openerTabId: sourceTabId, url });
    try {
      await recordTab(tabId);
      await save();
    } catch (err) {
      emit(tabId, "rec.detached", { reason: `could not record the new tab: ${err.message || err}` });
    }
  });

  resume();
  return { start, stop, status, isRecording: (tabId) => !!rec && rec.tabs.has(tabId), flush };
}
