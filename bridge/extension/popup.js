// Pairs this Chrome profile with the local bridge server: the shared token, and
// the label Claude addresses this browser by. One extension folder is loaded
// into every profile, so the label — not the install — is what tells them apart.

const tokenInput = document.getElementById("token");
const profileInput = document.getElementById("profile");
const status = document.getElementById("status");

function render({ profile, connected, hasToken }) {
  profileInput.value = profile || "";
  tokenInput.placeholder = hasToken ? "saved — paste again to replace" : "paste from setup";
  status.innerHTML =
    `<span class="dot ${connected ? "on" : "off"}"></span>` +
    (connected
      ? `connected as <b>${profile || "unnamed"}</b>`
      : hasToken
        ? "not connected — is the bridge server running?"
        : "no token yet — run the setup step Claude gave you");
}

async function refresh() {
  const state = await chrome.runtime.sendMessage({ op: "status" });
  render(state);
  // Pairing is a one-time step: keep it out of the way once it works.
  document.getElementById("pairing").open = !state.connected;
}

// ---------------------------------------------------------------- recording
//
// Record / Stop the tab this popup was opened on. On Stop the bridge server
// builds the folder; the popup copies a line for a Claude chat that points at it.

const $ = (id) => document.getElementById(id);

function show(id, on) {
  $(id).hidden = !on;
}

function message(text, error = false) {
  $("recMsg").textContent = text || "";
  $("recMsg").className = error ? "err" : "";
  show("recMsg", !!text);
}

async function copy(last) {
  await navigator.clipboard.writeText(last.handoff);
  await chrome.runtime.sendMessage({ op: "recCopied" });
  message("Copied — paste it into your Claude chat");
}

function renderRec({ now, last }) {
  const recording = now && now.recording;
  show("recStart", !recording);
  show("recStop", recording);
  show("recNow", recording);
  if (recording) {
    const who = now.meta && now.meta.by === "popup" ? "" : " (started by an agent)";
    $("recNow").innerHTML = "";
    const b = document.createElement("b");
    b.textContent = "● Recording";
    $("recNow").append(b, `${who}: ${now.title || now.url || "tab"} · ${now.steps} steps`);
    if (!$("recName").value && now.meta && now.meta.name) $("recName").value = now.meta.name;
  }
  show("recPath", !recording && last && last.ok);
  show("recCopy", !recording && last && last.ok);
  if (!recording && last) {
    if (last.ok) {
      $("recPath").textContent = last.path;
      if (!last.copied) copy(last).catch(() => message("Click Copy for Claude"));
    } else {
      message(`Could not build the folder: ${last.error}`, true);
    }
  }
}

async function refreshRec() {
  renderRec(await chrome.runtime.sendMessage({ op: "recState" }));
}

$("recStart").addEventListener("click", async () => {
  message("");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const out = await chrome.runtime.sendMessage({ op: "recStart", tabId: tab && tab.id, name: $("recName").value.trim() });
  if (!out.ok) return message(out.error, true);
  refreshRec();
});

$("recStop").addEventListener("click", async () => {
  $("recStop").disabled = true;
  message("Stopping and building the folder…");
  const out = await chrome.runtime.sendMessage({ op: "recStop", name: $("recName").value.trim() });
  $("recStop").disabled = false;
  if (!out.ok) return message(out.error, true);
  message("");
  $("recName").value = "";
  renderRec({ now: { recording: false }, last: out.result });
});

$("recCopy").addEventListener("click", async () => {
  const { last } = await chrome.runtime.sendMessage({ op: "recState" });
  if (last && last.ok) copy(last).catch((err) => message(String(err), true));
});

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (token) {
    await chrome.runtime.sendMessage({ op: "setToken", token });
    tokenInput.value = "";
  }
  const profile = profileInput.value.trim();
  if (profile) await chrome.runtime.sendMessage({ op: "setProfile", profile });
  setTimeout(refresh, 400);
});

for (const el of [tokenInput, profileInput]) {
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("save").click();
  });
}

refresh();
refreshRec();
// The step count moves while the popup is open.
setInterval(() => {
  if (!$("recNow").hidden) refreshRec();
}, 1500);
