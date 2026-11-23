---
name: change-roles
description: Edit Ansible roles, tasks, playbooks or filter plugins of this project without breaking its hard rules or its known traps. Covers the allowed modules, idempotency, check mode, no_log, the include_tasks loop trap, the delete-with-wait trap, drain behavior, NMState stamping, and the hostnqn unit whose bytes must not change. Use before and while touching anything under roles/, playbooks/ or plugins/, including a new task, a new wait, a changed text, or a "small" comment edit.
---

# Changing roles, playbooks and filters

## Hard rules (a change that breaks one is wrong, however well it works)
- Only `kubernetes.core` and `everpure.flasharray` modules, plus the core ones (`pause`, `assert`, `copy`, `set_fact`, `file`,
  `slurp`, `stat`, `include_vars`). No `command`, `shell` or `raw`. Inline `sh` is allowed only as container content (a probe
  Pod, a demo ConfigMap). If nothing can do a step declaratively, that step is the **breakpoint**, a person's, not a shell task.
- Node configuration is declarative (MachineConfig, NMState), found from the live cluster (a NIC by its MAC in
  NodeNetworkState). Nothing on a node by hand.
- The array is only read: `purefa_info` `network`, `hosts`, `pods`. Never `volumes` (it fails on Purity//FA 6.12). Never touch
  another cluster's realm or hosts.
- The token: `no_log: true` on every task that carries it; a helper never returns it, and an error never echoes it.
- Idempotent: a second run reports `changed=0`. Probes and check Pods are `changed_when: false`. Apply an object only when its
  spec differs (the NMState webhook stamps every NNCP update).
- Generic scenario: no platform names (hypervisor, port group, VM).

## Check mode
`--check` is a server-side dry run: nothing is created. Steps that need the result of a real step carry
`when: not ansible_check_mode`; an object in a namespace that does not exist yet needs a guard. The consent assertion on
`confirm` is `ansible_check_mode or confirm == mig_cluster` (in `nodes` it sits inside the real-run block). A gate belongs
before the first thing that drains or reboots: fail early with nothing changed.

## Traps measured on live runs
- **`include_tasks` in a loop is expanded up front, and a `when` on the include governs only the include.** It cannot stop
  later iterations once an earlier one succeeded (a no-op rerun of `prep` polled for 9 minutes). Put the `when` on a block inside
  the included file (`probe_wait_attempt.yml`), or wait with `until` and do one step per expected event (`roll_wait_node.yml`).
- **An `-e key=value` or `-e 'key=["a"]'` extra var is a string, never a list**, and `in` on a string is a substring test
  (`node10` selected `node1`). Take a list of node names from the operator only through `mig_pick_nodes`, resolved before the
  first change (as `rollback.yml` does), never with `selectattr('name', 'in', ...)`.
- `select()` takes Jinja tests, not filters. A `\n` in a Jinja string inside a folded YAML scalar is not unescaped: use block
  scalars for file contents.
- **`wait: true` on a delete by label of StatefulSet pods never finishes** (they return with the same labels at once). Delete
  without waiting and recognise the new pods by uid.
- `k8s_drain` needs `force` on control-plane nodes. Its `wait_timeout` is a courtesy (`mig_drain_wait`); the volume gate after
  the drain is the real wait.
- `spec.nodes[].env` does not restart the node plugin pod (delete it per node); a cluster-level `spec.env` change does. The
  plugin caches `nvme_core.multipath` at startup.
- `PURE_NVME_ALLOWED_IFACES` and `ENABLE_SUBNET_AWARE_NVME_LOGIN` crash the plugin when a node's SAN type is not `NVMEOF-TCP`.
- The plugin pod also has a `csi-node-driver-registrar` container: match the log container by exact name (`px-csi-node`,
  `node-plugin`, `px-node-plugin`).
- `nodes` must be resumable: a node that is done is skipped, one interrupted after the network change continues without asking.

## `roles/nvme_hostprep/files/nvme-unique-hostnqn.service`: do not touch
Its bytes are embedded in the MachineConfig; any change, a comment included, re-renders it and reboots the whole pool. Its
first comment line still names the tool it was copied from because running nodes carry exactly that text. Never order it
against multipathd (it made a boot cycle). To prove a change did not move it: `git diff --stat -- roles/nvme_hostprep/files`
is empty and `prep --check` on a prepared cluster reports `changed=0`.

## Operator texts
The three texts a person reads (start, network change, rollback) are defaults in `roles/nvme_migrate/defaults/main.yml`. After
any edit run `playbooks/tools/show_breakpoint.yml`, which renders them for every node.

Then follow `verify-change`.
