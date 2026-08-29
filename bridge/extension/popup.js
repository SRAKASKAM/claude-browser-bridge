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
  render(await chrome.runtime.sendMessage({ op: "status" }));
}

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
