---
name: verify-change
description: The done-gate for any change to this project (roles, filter plugins, playbooks, the config model, docker pins, compose, README). Runs the offline tests, a syntax-check of every playbook and a --check of every phase twice, the second at changed=0, and both run routes when the setup changed. Use before saying a change is finished, before a commit, and after any edit under roles/, plugins/, playbooks/, docker/ or compose.yaml, even a comment.
---

# Verifying a change

This tool drains and reboots nodes. A defect found at the breakpoint leaves a node drained, so everything that can be
caught without touching a node is caught here first. Report what actually ran and what could not (no live cluster, no
terminal): "done" means these steps passed, not that the change looks right.

In route A (Docker) prefix commands with `mig() { docker compose run --rm -T toolbox "$@"; }`; in route B run them
directly. Always pass `-e cluster=<name>` to a playbook.

## 1. Offline, no cluster needed
```bash
mig python3 tests/model_test.py          # last line must be: all ok
```
A new helper in `plugins/filter/` or a new validation in `mig_model` gets its own cases in `tests/model_test.py`: one that
accepts, and one refusal per rule. The test file loads `cluster.example.yaml`, so a stale example fails here.

## 2. Syntax of every playbook
```bash
for p in playbooks/*.yml playbooks/tools/*.yml; do mig ansible-playbook "$p" --syntax-check -e cluster=<name> || break; done
```

## 3. `--check` against a live cluster (read-only, server-side dry run)
Needs a config under `clusters/`, a kubeconfig and the array token (`FA_API_TOKEN` or `.state/fa-token`). Ask the
maintainer which cluster to use; never improvise one, and never copy its values into a tracked file.
- `migrate.yml --tags preflight` first: it is the read-only gate, and a failure there is a finding in itself.
- Then `--check` of every phase the change can reach: `--tags prep --check`, `enable`, `nodes`, `finalize`, `rollback`;
  `demo.yml --check`; the playbooks in `playbooks/tools/` that use the changed code. `nodes --check` only prints its plan.
- A change to a phase: run its `--check` twice. On a cluster where that phase is already done the first run is expected
  at `changed=0` already; on a cluster where it is not, the second run after a real run must be. Probe and check Pods are
  `changed_when: false`, so a nonzero `changed` is a task that is not idempotent.
- **`prep --check` on an already prepared cluster must report `changed=0`.** A change on the MachineConfig means the
  rendered bytes moved, which would reboot the whole pool: stop and look for an edit in
  `roles/nvme_hostprep/files/nvme-unique-hostnqn.service` or in how the Ignition file is built.
- A namespace that does not exist yet cannot be dry-run created: guard such tasks (as `nvme_demo` does).
- A `--check` that needs `confirm` is a bug: only the real runs of `prep`, `nodes`, `finalize`, `rollback` ask for it.

## 4. Both routes when the setup changed
Anything in `docker/`, `compose.yaml`, `ansible.cfg` or the pins: route A (`docker compose build`, then steps 1-3) and
route B (a fresh virtual environment with `pip install -r docker/requirements.txt` and
`ansible-galaxy collection install -r docker/requirements.yml`, then steps 1-3). The two routes must give the same results.

## 5. Keep the words true
Look for what the change makes stale: the README's input list, phase table, troubleshooting table and pins line,
CLAUDE.md's rules and gotchas, `cluster.example.yaml`'s comments, the texts in `roles/nvme_migrate/defaults/main.yml`
(after changing those, run `playbooks/tools/show_breakpoint.yml`, which renders them for every node).

Then, if the change is going to GitHub, continue with the `publish` skill.
