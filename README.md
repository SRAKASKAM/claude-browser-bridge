# claude-browser-bridge

Let Claude Code drive **the browser you already use** — your real Chrome
profile, already logged into everything — instead of a fresh automated browser
that every site treats as a robot.

```
Claude ──▶ local server (127.0.0.1) ──▶ Chrome extension ──▶ your real tab
```

## Why not just use Playwright?

A Playwright/CDP browser announces itself. It runs with `--remote-debugging-port`,
it sets `navigator.webdriver`, and it starts from a blank profile with no
history and no cookies. Cloudflare Turnstile, marketplace anti-bot systems and
bank login pages all read exactly those signals, and you spend the afternoon
fighting challenges instead of doing the task.

Here there is no automated browser at all. There is *your* browser, with a small
extension in it. Input is dispatched through `chrome.debugger`'s `Input.*`
domain, so pages receive real browser-level events (`isTrusted: true`), not
synthesised DOM events. And because it is your own profile, you are already
logged in — there is no login to automate.

## Install

In Claude Code:

```
/plugin marketplace add nkvch/claude-browser-bridge
```

```
/plugin install browser-bridge@claude-browser-bridge
```

Then just tell Claude **"set up the browser bridge"**. It runs the setup,
generates the pairing token, and walks you through the one part no script is
allowed to do — loading the extension into Chrome (three clicks). After that,
ask it to open a page and it will.

Requires [`uv`](https://docs.astral.sh/uv/) and Google Chrome. Nothing else —
the scripts declare their own Python dependencies.

## What setup actually does

1. Creates `~/.claude-browser-bridge/` — token, pins, screenshots. Deliberately
   **outside** the plugin, so a plugin update cannot wipe your pairing, and no
   secret ever sits inside a repo checkout.
2. Copies the extension to `~/.claude-browser-bridge/extension`. Chrome derives
   an unpacked extension's identity from its folder path, so it has to be a
   stable path rather than the versioned plugin directory.
3. You do: `chrome://extensions` → Developer mode → **Load unpacked** → that
   folder → click the toolbar icon → paste the token → name this profile.

**Several Chrome profiles?** Load the extension in each one, same token,
different label ("private", "work", ...). Claude addresses each browser by its
label, and refuses to guess when more than one is connected — because guessing
means typing into the wrong logged-in identity.

## Read this before you install it

This is a developer tool that hands an AI agent the keys to a browser that is
logged into your email, your bank and your messages. Be clear-eyed about it:

- The extension holds `<all_urls>` host permissions and the `debugger`
  permission. That is **full read and write access to every tab in that Chrome
  profile**, including the ability to read page contents and send input.
- The only thing standing between that and any other program on your machine is
  a random pairing token and the fact that the server binds to `127.0.0.1`.
  Anything running as your user can read that token.
- Load the extension only into profiles you are willing to expose. If you would
  not want an agent reading a tab, keep that tab in a different Chrome profile
  that has no extension in it.
- Chrome will show a "debugging this browser" banner while a tab is attached.
  That banner is doing its job — it means input really is going through the
  debugger. Do not look for a way to hide it.
- The bundled skill tells Claude to confirm the identity before typing, never to
  enter credentials or one-time codes, and to ask before anything irreversible.
  That is guidance to a model, not a sandbox. It is not a substitute for
  watching what it does with your accounts.

If that trade is not one you want to make, this tool is not for you, and that is
a perfectly reasonable conclusion.

## What's in here

| Path | What it is |
|---|---|
| `bridge/server.py` | Local HTTP↔WebSocket router, one server for many profiles |
| `bridge/bb.py` | The CLI Claude actually calls |
| `bridge/extension/` | The MV3 extension source (read it — you are installing it) |
| `bridge/setup.sh` | Token, state dir, extension sync |
| `skills/browser-bridge/` | The skill that teaches Claude to use all of the above |

## Troubleshooting

**`bridge server not running`** — the server process isn't up. Claude is
supposed to start it itself; you can too:
`uv run ~/.claude/plugins/…/bridge/server.py`.

**`extension_connected: false`** — Chrome side. The extension is disabled, that
profile isn't open, or the token was never pasted into the popup.

**Clicks do nothing, but the reply says `ok`** — check `trusted` in the reply. If
it is `false`, DevTools is open on that tab. Chrome allows one debugger client
per tab, so the extension lost the attach and quietly fell back to synthetic
events. Close DevTools.

**After a plugin update** — re-run setup so the extension folder re-syncs, then
reload the extension in `chrome://extensions`. Your token and labels survive.

## License

MIT
