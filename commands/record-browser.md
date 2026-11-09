---
description: Record a browser session in the user's own Chrome until they say "stop", then sum up the report
argument-hint: "[profile] [what to look at] [--tab ID] [--out DIR]"
---

Record what the user does in their own Chrome, through the browser bridge, so
it can be debugged or investigated from the recorded folder afterwards.

Arguments: `$ARGUMENTS`. A word that matches a connected profile (see below)
is the profile; the rest, if any, says what is being looked at and becomes the
recording's name. Pass `--tab ID` or `--out DIR`, if given, on to `record start`.

```bash
BB="uv run ${CLAUDE_PLUGIN_ROOT}/bridge/bb.py"
```

PowerShell: `$BB = "uv run $env:CLAUDE_PLUGIN_ROOT\bridge\bb.py"`.

## Start

1. `$BB health` lists the connected profiles by label. Match the user's word
   to a label: exact, prefix or substring, case-insensitive. With one profile
   connected, use it. With several and no word naming one, ask which, offering
   the labels as options, before starting. No server or no profile connected:
   follow the setup section of the `browser-bridge` skill, or tell the user in
   one line what is missing.
2. `$BB record start --profile <label>`. It records the tab the user is
   looking at in their own window, plus tabs opened from it.
   - `ok: false` with a `fix` (a recording already running, a lost one): do
     what `fix` says, or tell the user in one line if it needs them.
3. Tell the user, in one line in their language, that recording is on in that
   tab (its title from the result) and to say "stop" when done. Then end the
   turn and wait. Do not start before this command was run, and do not drive
   the tab yourself while it records.

## Stop

When the user says stop ("стоп", "stop", "готово", "done"):

1. `$BB record stop --profile <label> --name "<what is being looked at>"`.
   The name is the argument's rest, else a few words from what the user said
   about it; leave `--name` out if there is nothing.
2. Read `report.md` from the result's `path`: errors first, then Slowness,
   then the steps. Open a step's screenshot or DOM only if the report alone
   leaves the gist unclear.
3. Reply in the user's language, 2-3 lines: what was recorded (steps, pages),
   what stands out (errors, failed requests, slow frames with their cause),
   and, if it is clear, the likely culprit. Then a link to the report as
   `[report.md](<path>/report.md)` and the folder path. No step-by-step dump.
