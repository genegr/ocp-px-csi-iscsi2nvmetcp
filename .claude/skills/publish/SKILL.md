---
name: publish
description: Commit and push this project to its public GitHub repository without leaking anything of the maintainer's site and without weakening the legal text. Scans tracked files for environment values and secrets, checks the disclaimer, trademark attribution and license, the hostnqn unit and line endings, then commits and pushes only on the owner's go-ahead, and tags releases. Use before any commit that will be pushed, before any push, when a new kind of file is added, when the README's claims or disclaimer change, and when the repository's About text is edited.
---

# Publishing

The repository is public and its history is the published history. A value that gets in is not removed by a later commit
(that takes a history rewrite, which is the owner's decision). So the check comes before the commit, and a commit or push
happens only when the user asks for it.

## 1. What is tracked
`git status` and `git ls-files`. `.gitignore` keeps out `clusters/*` (except its README), `.state/`, `.sync.env`, `docs/`
(working notes and live-test evidence of the maintainer's lab) and Python caches. A new kind of file (a log, an export, an
evidence report) is either ignored or reviewed like the rest.

## 2. Scan for site values and secrets (use the Grep tool on tracked and staged files)
- IPv4 literals: `\b\d{1,3}(\.\d{1,3}){3}\b`. Allowed: 192.0.2.x, 198.51.100.x, 203.0.113.x, 198.18.x and 198.19.x, netmasks,
  `0.0.0.0`, `127.`, Docker's default `172.17.0.0`. Anything else is a finding, unless it is an invented test fixture that does
  not occur in the maintainer's private config (check by match count, never by printing the config); new fixtures use the
  documentation ranges. Version numbers such as `26.3.0` have three parts and are not.
- Hosts and URLs: `https?://` (public documentation, GitHub and `example.com` are fine) and `\.(local|lan|corp|internal)\b`.
- Secrets: `api_token|password|passwd|secret|BEGIN [A-Z ]*PRIVATE KEY|eyJ|certificate-authority-data|client-key-data`.
  A variable *name* is fine; a value never is.
- MAC addresses: `([0-9a-f]{2}:){5}[0-9a-f]{2}`. Only the example's `02:00:00:00:..` ones are allowed.
- Site words the maintainer knows: the lab's cluster name, array address, jump host and user names, platform product names
  (hypervisor, port group). Ask the maintainer for the list or read it from their private notes. **Never write those strings
  into a skill, a test or any tracked file**: a scan list kept in the repo would itself be the leak.
- Also read what a pattern cannot know: prose in README and CLAUDE.md that names an internal system, a person or a ticket.

## 3. The legal text stays intact
- README opens with the **Disclaimer**: not provided or supported by Everpure, Inc.; used at the user's own risk; Everpure not
  responsible in any manner. Wording changes are the owner's call (a legal review is advisable). Do not soften it.
- Trademark symbols at the **first** use of each product name (and the attribution paragraph that names the owner of each mark).
  A new product name in the README gets its mark and its owner in that paragraph; ask the owner when a mark is uncertain.
- `LICENSE` is the Apache License 2.0, verbatim; the README's License section points to it.
- The GitHub About description and topics are repository settings, not files. Keep them consistent with the disclaimer:
  "unofficial", "not provided or supported by Everpure, Inc.", "use at your own risk".

## 4. Claims match reality
"Tested with" and the statement that `rollback` has not been run live match what has been run. The phase table matches the tags
in `playbooks/migrate.yml`. The README's pins line equals `docker/requirements.*`.

## 5. Bytes
`.gitattributes` forces LF everywhere. `git ls-files --eol` must show `i/lf` for every file (on Windows too).
`git diff --stat -- roles/nvme_hostprep/files` must be empty unless a change there is the point, because those bytes are in the
MachineConfig that reboots the pool.

## 6. Verify, commit, push
1. `verify-change` is done.
2. Commit only when the user asks. Subject in the imperative and about what a user sees (see `git log`); end the message with
   the attribution line the session specifies. New commits on top; never amend or rewrite what was pushed.
3. Push only when the user asks: `git push origin main` (SSH remote). A push is outward-facing and may be cached or indexed
   even if deleted later.
4. A release: an annotated tag `vX.Y.Z` after the push, when the owner asks. Notes: what a user sees change, and the
   "Tested with" delta.
