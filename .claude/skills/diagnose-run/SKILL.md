---
name: diagnose-run
description: Triage a failed, interrupted or surprising run of the migration, such as a node left drained or cordoned, volumes that will not detach, a preflight that refuses to start, MachineConfigPools paused by prep, a node plugin that crashes or runs in the wrong multipath mode, a drain timeout warning, or array and secret errors. Then record what was learned in the right place. Use whenever a playbook run ends with failed=, a node is cordoned, or the user pastes an error from this tool.
---

# Diagnosing a run

The rule is look first, change nothing to "see what happens". A node left drained or cordoned is the usual damage, and
the next run refuses to start while it is.

## 1. Read-only look
- `playbooks/verify.yml -e cluster=<name>` and `migrate.yml --tags preflight`. The preflight's refusal names the failing check.
- Anything else you need to see: a throwaway playbook in `.state/scratch/` (gitignored, and inside the project, so the toolbox
  mounts it) using `kubernetes.core.k8s_info` with `module_defaults` like `playbooks/migrate.yml`. There is no `oc`, `kubectl`
  or shell here by rule. In the container the kubeconfig path must be absolute (`/work/.state/kubeconfig`); to import the
  filter plugins from Python in the container set `PYTHONPATH=/usr/share/ansible/collections`.
- `playbooks/tools/array_hosts.yml` (the array's host objects of this cluster), `attach_check.yml` (test Pods and PVCs on a
  node), `check_prepared.yml` (the per-node checks of `prep`).

## 2. Symptom, cause, fix
Fixes that change the cluster need the user's go-ahead and a declarative module (for an uncordon,
`kubernetes.core.k8s_drain state=uncordon`), never a hand edit.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `nodes` stops at "no PX volume attached to the node", node drained | The network behind the storage NICs was changed before the breakpoint asked. The iSCSI paths are gone, the plugin's `NodeUnstageVolume` fails (`open /dev/sdX: no such device or address`), the kubelet keeps the volumes in `volumesInUse` and the VolumeAttachments stay. | Put the network back (the person's step); the next kubelet retry detaches within minutes. Never clear iSCSI sessions by hand: they outlive the last volume and are harmless. |
| Preflight refuses: `authentication`, `openshift-apiserver`, `machine-config` degraded | A node was left cordoned for about 30 minutes. On a three-node control plane those operators keep one replica per node (hard anti-affinity), so one replica of each stays Pending. | Uncordon the node, wait about a minute until the operators settle, re-run `nodes`: it drains the node again. |
| `timeout reached while pods were still running` after the drain | With `force` the module deletes finished Pods; the etcd, kube-apiserver and kube-scheduler operators recreate `revision-pruner-N-<node>` under the same name, and the wait counts them as running. | Harmless. The volume gate after the drain is the real wait. Raise `-e mig_drain_wait=900` only for workloads with a long termination grace period. To see why, call the module's `filter_pods` read-only. |
| `stdin is not a terminal` at "start?" | No terminal. Nothing was changed. | Run `nodes` with one (route A without `-T`, `ssh -t`). |
| `prep` paused the pools | A node came back wrong; the tool paused the pools before the next reboot. | Fix the node; `--tags prep -e confirm=<name>` again. |
| `prep --check` shows a MachineConfig change on a prepared cluster | The rendered bytes moved, which would reboot the pool. | Find the edit (`roles/nvme_hostprep/files/`, the Ignition builder); revert it. |
| `prep` rerun polls for minutes with nothing to do | An `include_tasks` loop where a `when` on the include does not stop later iterations. | See the `change-roles` skill. |
| Node plugin crashes after an env change | `PURE_NVME_ALLOWED_IFACES` or `ENABLE_SUBNET_AWARE_NVME_LOGIN` set at cluster level while a node is still on iSCSI. | Remove them from the cluster env; they belong in a node override only. |
| Plugin runs in DM mode though native was expected | It reads `nvme_core.multipath` once at startup; `nvme_core` was not loaded, or the setting changed after. | `nvme_tcp` must load at boot (`prep`); delete the node's plugin pod after any change. |
| "the secret does not list your endpoint", or the array refuses `network` | `flasharray.endpoint` differs from the `px-pure-secret`; or the token is realm-scoped. | Fix the config; use a Storage Admin account's token. |
| `purefa_info` `volumes` fails | Purity//FA 6.12 (#1103). | This project never asks for it; keep it that way. |

## 3. Record what was learned
A new finding goes where the next person needs it, with no site values (no cluster, host or array names, no addresses):
- **CLAUDE.md "Gotchas"**: what was measured, why, and what to do. Mark a guess as a guess.
- **README "If something goes wrong"**: only if a customer could hit it: symptom, cause, fix in a line or two.
- **A test** in `tests/model_test.py` when the cause was logic in a filter or the model.
- **A gate**: when it could have been caught before anything changed, make the preflight or the task stop early with nothing touched.

Then follow the `verify-change` skill.
