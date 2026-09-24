# Research notes: how Claude Desktop / Claude Code store sessions on Windows

Method: (1) read public reports, (2) inspect the actual files on the reference machine, (3) read the storage
code in the *installed* Claude Desktop 2.7032.0 (MSIX) main-process bundle (`app.asar`, read-only), and
(4) treat every community claim as a hypothesis until confirmed by (2) or (3).

## What the public sources say

| Source | Claim |
|---|---|
| [anthropics/claude-code#48511](https://github.com/anthropics/claude-code/issues/48511) | Switching accounts in Desktop makes all Code/Cowork history "disappear" (closed *not planned*). |
| [DEV: "Claude Desktop history missing after switching accounts…"](https://dev.to/vitaliyhayda/claude-desktop-history-missing-after-switching-accounts-and-how-to-get-it-back-4a26) | Transcripts live once in `~/.claude/projects/…/<uuid>.jsonl`; per-account *records* live in `claude-code-sessions/<account>/<org>/local_<id>.json`; copying a record into another account's folder (Desktop closed) brings the session back; Desktop keeps records in memory and rewrites them; Remote-Control links (`bridgeSessionIds`) stay with the original account. |
| [#74662](https://github.com/anthropics/claude-code/issues/74662) (tracking), [#18435](https://github.com/anthropics/claude-code/issues/18435) (account profiles), [#29373](https://github.com/anthropics/claude-code/issues/29373) (migration from `local-agent-mode-sessions` to `claude-code-sessions`), [#79810](https://github.com/anthropics/claude-code/issues/79810) | Same account-scoping problem seen from other angles; no official cross-account sync. |
| [Claude Code docs: Desktop](https://code.claude.com/docs/en/desktop) | Each Code-tab conversation is a session with its own history and project folder. |

Nothing above describes the **MSIX** layout, tombstones, or which record fields are account-specific. Those
had to be found on the machine.

## Verified on this machine (Windows 11, Claude Desktop 2.7032.0 MSIX)

| Claim | Result | Evidence |
|---|---|---|
| Transcripts are shared, not per account | **Confirmed** | `~/.claude/projects/<encoded cwd>/<cliSessionId>.jsonl`; encoding = every non-alphanumeric char → `-` (`D:\Best Projects\AI SECURITY` → `D--Best-Projects-AI-SECURITY`). Each transcript contains exactly one `sessionId` (its own). Sidecar folder `<cliSessionId>/` holds `subagents/`, `tool-results/`, `custom-title.json`. |
| Per-account records | **Confirmed** | `…\claude-code-sessions\<accountUuid>\<orgUuid>\local_<uuid>.json` (compact JSON, ≤10 MB, UTF-8). |
| Data location on MSIX | **New finding** | Physical folder is `%LOCALAPPDATA%\Packages\Claude_<pfn>\LocalCache\Roaming\Claude`. Processes started by Claude Desktop (Code-tab shells, the CLI…) see it through file virtualisation as `%APPDATA%\Claude` (identical NTFS file ids). A normal process may see a different/stale `%APPDATA%\Claude`. The tool therefore always uses the physical package path and reports aliasing. |
| Local id ≠ CLI id | **Confirmed** | Records carry both `sessionId` (`local_…`) and `cliSessionId`. For sessions Desktop *adopts* from a transcript the local id is `local_<cliSessionId>`. |
| Desktop only loads the signed-in account/org folder, once, at start | **Confirmed in code** | `loadSessionRecords()` reads `<userData>/claude-code-sessions/<currentAccountId>/<currentOrgId>` only; records are debounced-written (250 ms) from memory. Hence: never write while Desktop runs. |
| Other account's sessions are hidden and can't simply be re-imported | **Confirmed in code** | The "recover/resume CLI session" scan treats any CLI id recorded by **any** account/org (also `priorCliSessionIds`, `unarchivedCliSessionId`, and every tombstone) as already owned. |
| Tombstones | **Confirmed, richer than reported** | File `deleted_<id>` inside the org folder, content = epoch ms. Written on UI delete for the local id **and its CLI lineage ids**. The app's own import clears tombstones for the imported id in all orgs of the account. |
| Records contain per-org data | **New finding** | `remoteMcpServersConfig` (≈200 KB: the account's connector list with tool schemas), `enabledMcpTools` (keyed by connector uuid), `promptAppendSnapshot`, `toolSurfaceSnapshot`. Copying a record verbatim (community advice) transplants account A's connector snapshot into account B. `remoteMcpServersConfig` is read with `?? []`, and Desktop's own import path creates records without any of them → safe to omit. |
| Limit errors are persisted | **New finding** | `error: "You've hit your session limit · resets …"`, `errorAt`, `priorErrorMark` live in the record. Copying them would show the target session as failed. |
| Lineage | **New finding** | `priorCliSessionIds` records earlier CLI ids when a conversation is resumed/rewound/compacted under a new CLI id (e.g. *Context Rot*: 4 prior ids; only the current transcript remains on disk). `cliSessionId` can therefore *change* over time – sync must follow it. |
| Delete side effects | **New finding** | Deleting a session writes `<cliId>.desktop-released.json` (`{"v":1,"releasedAt":…,"reason":"delete"}`) beside the transcript, which lets Claude Code's sweep (or Desktop's reaper) remove the transcript. Seen on the machine for a deleted Context-Rot lineage session. **Consequence:** deleting a *synced* session in one account can endanger the transcript the other account still needs → the tool refuses to sync sessions that carry such a marker unless told otherwise. |
| Live sessions | **New finding** | `~/.claude/sessions/<pid>.json` (`sessionId`=CLI id, `hostSessionId`=local id, `status`, `procStart` FILETIME) is a live-process registry; validated against the process start time to survive pid reuse. |
| Other files | Confirmed | `archived-sessions.idx` (optional archive hint), `scheduled-tasks.json`, `<name>.json.unreadable-<ms>` (quarantine of a record Desktop could not parse), `imported-staging/`. |
| Which account is signed in | Confirmed | `config.json` → `lastKnownAccountUuid` (read alone; the OAuth token cache in the same file is never read or copied). |
| Account ↔ organisation pairing | Confirmed | `local-agent-mode-sessions/skills-plugin/<orgUuid>/<accountUuid>/` exists for each pair that has been used. One stray `86396211…/0ff3c8d7…` folder (only `scheduled-tasks.json`) has no such evidence and is reported as *weak pairing*. |
| E-mail addresses are not stored by Desktop | Confirmed | Only the CLI login (`~/.claude.json` → `oauthAccount`) carries e-mail for *its* account; the tool reads just uuid/e-mail/name from it and lets you set labels. |

## Design consequences

1. **The transcript is never copied.** One conversation, N account-specific records pointing at it. Copying a
   transcript would fork history and break the one-to-one relation between conversation and CLI id.
2. **Target record = sanitised subset**, not a file copy (see README "field policy").
3. **3-way merge on repeat syncs** using a ledger of what was last written, so renames made in the target
   survive and only genuinely newer source progress flows across; lineage (`priorCliSessionIds`) decides who is ahead.
4. **Tombstones are authoritative.** A tombstone naming the CLI id, the local id, or the intended target id in
   the target account blocks creation (unless an explicit `--resurrect`).
5. **Write only with Desktop closed**, verify by independent re-read, and keep pre-images for rollback.
6. **Schema is discovered, not assumed:** field classification is data-driven; unknown fields are dropped and
   listed; a probe checks that the installed bundle still contains the storage constants this tool relies on.

## Verified after the first real sync (2026-09-23)

* The synthesised record for *AI security project completion* (`local_48c9ce30-…` in account 188b9fd5) was displayed by
  Desktop in that account's Code tab and opened by the user. Desktop then rewrote the record itself and re-created the
  organisation-specific snapshot fields (`remoteMcpServersConfig`, `promptAppendSnapshot`) for the target account – i.e.
  it treats the record as its own. `verify` reports the file as "modified since (normal after use)".
* Same conversation: source and target records share `cliSessionId`; the project folder holds exactly one transcript
  (`48c9ce30-….jsonl`, one `sessionId` throughout) and it was byte-identical after the sync.
* Still to observe by the user: the first *new turn* after resuming under the other account (the API side of the resume
  is outside anything this tool can inspect). The transcript is the single source of truth: after that turn its size and
  the record's `completedTurns`/`lastActivityAt` move on and the next sync carries them back.

## Not verifiable from outside Desktop

* Whether closing Desktop's window always ends the process or can leave it in the tray. The main-process bundle contains
  a `window-all-closed` handler but no tray persistence that could be identified by static scan; automatic mode simply
  waits for the process list to show no Desktop process and the GUI/`auto status` show whether one is still running.
