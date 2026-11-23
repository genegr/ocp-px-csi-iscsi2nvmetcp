---
name: run-phase
description: How to run a phase of the migration (preflight, prep, enable, nodes, finalize, rollback, demo) against a live cluster the way this project demands. Covers the read-only look first, the --check plan shown to the user, the user's own go-ahead in reply to that plan, -e confirm, a terminal for nodes, a background log for prep, show_breakpoint before nodes, and the proof afterwards. Use whenever the user says run, start, go or continue for any phase, or asks what the next step of the migration is.
---

# Running a phase

Every phase except `preflight` changes a production cluster and some wait for a person. The rules are in CLAUDE.md; this
is the order of work. Always `-e cluster=<name>`; without `--tags` only the read-only preflight runs. In route A
prefix with `mig() { docker compose run --rm -T toolbox "$@"; }` (drop `-T` for `nodes`).

## Consent (not negotiable)
`prep` (rolling reboot of every storage node), `nodes` (drains a node, waits for a person), `finalize` (restarts every
node plugin) and `rollback` need **the user's go-ahead in a reply to the plan you showed**, and then
`-e confirm=<cluster>`. A dictated "go" covers the steps, not the consent. A go for one phase, or one node, does not carry to
the next. Never put `confirm` on a command before the user has answered the plan.

## The order, for any phase
1. **Where it stands**: `playbooks/verify.yml -e cluster=<name>` (read-only: stage, labels, StorageNodeInitiator per node,
   addresses, the demo) and `migrate.yml --tags preflight`. If the preflight refuses, stop and use the `diagnose-run` skill:
   nothing has been changed yet, and it is better to know why.
2. **The plan**: `--check` of the phase. Show the user what it says in plain words: which objects change, what restarts,
   how long it takes, what they will have to do.
3. **Ask** for the go-ahead, naming the cluster and the phase. Wait for the reply.
4. **Run** with `-e confirm=<cluster>`.
5. **Prove it**: the phase's own report, `verify.yml`, and a second run of the same phase must report `changed=0`.

## Per phase
| Phase | Notes |
| --- | --- |
| `prep` | Rolls every pool node one at a time through the MCO, roughly 10-15 minutes per node (an estimate). Run it in the background (`run_in_background`, `-T`) with the output in a log under `.state/` and wait for the notification instead of polling. A node that fails its check makes the tool pause the pools: fix the node and run `prep` again, it unpauses what it paused. `playbooks/tools/check_prepared.yml` re-checks the nodes on demand. |
| `enable` | Restarts nothing. Writes `.state/<name>/storagecluster-before.yaml` (first copy kept: it is the rollback's input, never delete it). |
| `nodes` | 1) Run `playbooks/tools/show_breakpoint.yml` first: it renders the operator's three texts for every node, and a template error found at the pause would leave a node drained. 2) It needs a terminal: the harness shell has none, so hand the user the exact command (route A without `-T`, over `ssh -t` from a workstation). Without a terminal the run stops at "start?" with the node untouched. 3) Start with one node: `-e migrate_nodes=<node>`. Names are matched exactly and a name that is not in the config stops the run before anything is touched (`mig_pick_nodes`). 4) After each node read the evidence report. The person at the breakpoint changes the network; you never do, and never touch a node by hand (`iscsiadm`, `nvme`, `nmcli`). |
| `finalize` | Every node plugin restarts. Afterwards `demo.yml --tags bounce,verify` re-attaches every demo volume (now over NVMe/TCP) and checks the data. |
| `rollback` | Not yet run on a live cluster: say so, run it first on one node (`-e rollback_nodes=<node>`) of a non-critical cluster, `--check` first. It needs `.state/<name>/` of the migration. |
| `demo.yml` | `deploy` and `verify` are safe; `teardown` deletes the namespace and its volumes and needs `-e confirm=<demo namespace>`. |

## Secrets and files
The array token comes from `FA_API_TOKEN` (forwarded into the container) or `.state/fa-token`, never from the YAML and
never on a command line or in a log you paste. The kubeconfig stays under `.state/`. Keep `.state/<name>/` with the config
until the migration is closed.

## If the run stops
Do not re-run blindly: run `migrate.yml --tags preflight` first (a node left cordoned makes the next run refuse to start),
then follow the `diagnose-run` skill.
