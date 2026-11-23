# CLAUDE.md

Migration of PX-CSI from iSCSI to NVMe/TCP on a running OpenShift cluster, node by node, on one FlashArray. A self-contained
Ansible project: it reads one config (`clusters/<name>.yaml`), a kubeconfig and the array token, and needs **no other project or
directory**. Human docs: `README.md`.

## Hard rules
- **Only `kubernetes.core` and `everpure.flasharray` modules**, plus core ones (`pause`, `assert`, `copy`, `set_fact`,
  `file`, `slurp`, `stat`, `include_vars`). No `command`/`shell`. The only shell file is the optional `tools/sync.sh`. Inline
  `sh` in probe Pods and demo ConfigMaps is container content, not a task.
- **Nothing is installed on a jump box.** Run in the project's toolbox container (route A, built from `docker/`) or on an Ansible
  machine that meets `docker/requirements.{txt,yml}` (route B). A new dependency means a new pinned entry there.
- **Declarative and from the live cluster.** Node configuration is a MachineConfig or NMState policy; a NIC is found by its MAC in
  the node's `NodeNetworkState`; nothing is changed on a node by hand (no `iscsiadm`, `nvme`, `nmcli`, file edits). If a step
  needs hands, it is the breakpoint.
- **Generic scenario.** The migration is "the network behind each storage NIC changes (a VLAN change on the node's switch
  ports) while the node is drained". No platform name (hypervisor, port group, VM) in roles, filters or the model. What only a
  site knows goes in the config's free-text `operator_note`.
- **The array is only read** (`purefa_info`: `network`, `hosts`, `pods`; never `volumes`, #1103). PX creates and removes hosts
  and volumes. Never touch another cluster's realm or hosts.
- **The array: the config has its address and an account, the cluster has the rest.** `flasharray.endpoint` and the API token of
  a Storage Admin account (PX-CSI's own token may be realm-scoped and cannot list the array's network interfaces: measured, `purefa_info`
  `network` fails with it, `hosts` and `pods` work) are the config's. The realm comes from the `px-pure-secret` in the Portworx namespace
  (`portworx.namespace`, the setting that must be right), the pod from the StorageClass's `pure_fa_pod_name`; the run stops if the
  secret does not list the config's endpoint (the config must not be about another array than PX uses), and `flasharray.pod` or
  `.realm` in the config are refused (a copy could only disagree).
- **Secrets never on a command line and never in a tracked file**: the array token comes from `FA_API_TOKEN` (forwarded into the
  container, `-e NAME` style) or `.state/fa-token`, never from the YAML itself; the kubeconfig lives under `.state/` or wherever the
  config says. Tasks that carry the token use `no_log: true`; the helper that reads the px-pure-secret returns no token and puts none
  in an error.
- **Idempotent**: a second run of `prep`, `enable`, `nodes`, `finalize` reports `changed=0` (probe and check Pods are
  `changed_when: false`).
- **Published-safe**: no environment values in tracked files. `clusters/*` (except its README), `.state/` and `.sync.env` are
  gitignored; `cluster.example.yaml` uses documentation addresses (RFC 5737, and 198.18.0.0/15 for the iSCSI side) and example.com.
- **Versions**: stay on the GA PX-CSI the cluster runs (>= 26.3.0, multi-SAN). NVMe multi-interface needs >= 26.4.0 (only
  `26.4.0-ea1` exists): out of scope. Pins in `docker/` are exact.

## Run model (two ways; README has the full text of both)
**A. Docker**: `docker compose build` once, then `docker compose run --rm -T toolbox ansible-playbook
playbooks/migrate.yml -e cluster=<name> --tags preflight`. The project is mounted at `/work`; `ansible.cfg` has relative paths.
`bash tools/sync.sh` copies it to the jump box (`.sync.env`: `SYNC_HOST`, `SYNC_DIR`, `SYNC_SSH_OPTS`).
**B. A plain Ansible machine**: `pip install -r docker/requirements.txt`, `ansible-galaxy collection install -r
docker/requirements.yml`, then `ansible-playbook` from the project directory.

Either way: run the `nodes` phase with a terminal (A: no `-T`, over `ssh -t`): the breakpoint is `pause`. The playbook asks
"start?" with a `pause` before it drains anything, so a run without a terminal stops with the node untouched. Long runs (`prep`)
go in the background with the output in a log. Always pass `-e cluster=<name>`. A change to a route's setup is tested on both.

## Phases and gates (`playbooks/migrate.yml`; `playbooks/demo.yml`; `playbooks/verify.yml` is read-only)
`preflight` (default, read-only) → demo baseline → `prep` → `enable` → `nodes` → `finalize` (→ `rollback`, `never`).
- Every real run is preceded by `--check`. **`prep` (rolling reboot of all storage nodes), `nodes` (drains a node and waits for a
  person), `finalize` (restarts every node plugin) and `rollback` need the user's go-ahead in a reply to the shown plan, and
  `-e confirm=<cluster>`.** A dictated "go" covers the steps, not the consent.
- `demo.yml --tags teardown` needs `-e confirm=<demo namespace>`.
- Before `nodes`, run `tools/show_breakpoint.yml`: it renders the operator's texts for every node; a template error found at the
  pause would leave a node drained.

## State outside this project
The network change is made outside Ansible. Whatever else describes the nodes' storage networks (an IaC state, an inventory,
NMState sources, a CMDB) must be updated to the `target` networks and addresses of the config afterwards, and nothing may
re-apply the old ones in between (it would put NICs, NMState policies or the StorageCluster env back to iSCSI).

## The starting state is recorded here, or it is lost
The old network may be written nowhere else, so a rollback depends on what this project keeps. The config is the initial
inventory: the model requires `networks.current`, each NIC's MAC and current address, and `migration.operator_note_rollback` (the
physical network the NICs leave and who puts it back; a blank one is refused), and the preflight compares the addresses with the
live nodes. `.state/<name>/` holds `storagecluster-before.yaml` (`enable`, first copy kept) and `before/<node>.yaml` (`nodes`,
before a node's network is touched: its storage NICs' interfaces and the NMState policy, first copy kept, nothing moves without
it). Rollback restores that policy; without a snapshot it rebuilds one from `networks.current`. Never delete `.state/<name>/`
before the migration is closed, and keep it with the config.

## Gotchas (from the official docs and runbooks, confirmed or corrected by the live runs)
- **The node plugin reads `nvme_core.multipath` once at startup and caches it**; if `nvme_core` isn't loaded it silently
  falls back to DM mode. So `nvme_tcp` loads at boot (M2) and the plugin pod restarts after any change (M4 step 7).
- **`spec.nodes[].env` does not restart the node plugin pod**: delete it per node. A cluster-level `spec.env` change does.
- **Draining first is mandatory**: a host registered with NVMe credentials cannot connect to a volume previously attached via
  iSCSI. Wait for no VolumeAttachment, no volume on the node's array host and no iSCSI LUN before the network change. Sessions
  alone prove nothing: they outlive the last volume (a test node kept four after an earlier smoke test) and are harmless;
  never clear them by hand.
- **PX creates a node's array host on its first attach and removes it after the last detach** (hosts are `<realm>::<node>-<uuid>`;
  the test cluster had none while idle). `purefa_info hosts` returns `iqn`, `nqn` and `volumes` per host.
- On RHCOS 4.22.22 the nodes already had distinct hostnqns (derived from the SMBIOS UUID); the unit in `nvme_hostprep/files`
  makes that deterministic. A cloned image elsewhere may share one NQN (one array host object): the unit fixes that. Never
  order it against multipathd (boot cycle).
  **Never edit that file casually, not even a comment**: its bytes are in the MachineConfig, and any change re-renders it and
  reboots the whole pool (its first comment line still names the tool it was copied from, because nodes already running carry
  exactly that text; a `--check` caught the rewrite).
- **`PURE_NVME_ALLOWED_IFACES` / `ENABLE_SUBNET_AWARE_NVME_LOGIN` crash the node plugin when the node's SAN type isn't
  `NVMEOF-TCP`**: never at cluster level while any node is on iSCSI.
- **The node plugin pod has a `csi-node-driver-registrar` sidecar**: the log container is matched by exact name
  (`px-csi-node`, `node-plugin`, `px-node-plugin`: the docs disagree).
- Without multi-interface PX connects once per array NVMe/TCP portal and the kernel picks the NIC; with the NICs on separate
  subnets each portal is reached through its own NIC. `PURE_NVME_ALLOWED_CIDRS` filters the array's portals only.
- `purefa_info` `volumes` fails on Purity//FA 6.12; `purefa_info` `hosts` reports a host's volumes (when it didn't, the
  VolumeAttachments would be the evidence).
- **`include_tasks` in a loop is expanded up front, and a `when` on the include governs only the include**: it cannot stop later
  iterations once an earlier one succeeded (a no-op rerun of `prep` polled 180 times for 9 minutes). Put the `when` on a block
  inside the included file (`probe_wait_attempt.yml`), or wait with `until` and do one step per expected event
  (`roll_wait_node.yml`). `select()` takes Jinja tests, not filters. A `\n` in a Jinja string inside a folded YAML scalar is
  not unescaped: use block scalars for file contents.
- **`wait: true` on a delete by label of StatefulSet pods never finishes** (they come back with the same labels at once; the
  demo `bounce` waited its whole timeout and failed). Delete without waiting and recognise the new pods by uid.
- **`--check` is a server-side dry run**: nothing is created (verified), but an object in a namespace that does not exist yet
  cannot be dry-run created (guard it, as `nvme_demo` does).
- **A node left cordoned by a failed or interrupted `nodes` run blocks the re-run after about 30 minutes**: on a three-node control
  plane `openshift-apiserver`, `oauth-apiserver` and `oauth-openshift` keep one replica per node (hard anti-affinity), so one replica
  of each stays Pending, `authentication`, `openshift-apiserver` and `machine-config` go Degraded and the preflight health gate
  refuses to start (nothing is changed). Uncordon the node (`kubernetes.core.k8s_drain state=uncordon`, the user's go), wait until the
  operators settle (about a minute), then re-run: it drains the node again. Moving the storage NICs' network before the node's volumes
  are detached cuts the iSCSI paths: the plugin's `NodeUnstageVolume` fails (`open /dev/sdX: no such device or address`), the kubelet
  keeps the volumes in `volumesInUse` and the VolumeAttachments never go away; putting the network back is the fix (the next kubelet
  retry succeeds).
- **The drain's own wait (`mig_drain_wait`, default 120 s) is a courtesy, and running out is not a failure**: with `force: true` the
  module also deletes finished Pods with a non-controller owner, the etcd, kube-apiserver and kube-scheduler operators recreate
  `revision-pruner-N-<node>` under the same name on the node, and `wait_for_pod_deletion` counts a Pod as gone only when it is Not Found
  or runs elsewhere; it then ends with the warning `timeout reached while pods were still running` (on a three-node control plane one
  drain used the whole 900 s it was then given, 15 min, another took under 4). The volume gate after the drain is the real wait. Raise it only for workloads with a long
  termination grace period (`-e mig_drain_wait=900`). Diagnose by calling the module's `filter_pods` read-only.
- `kubernetes.core.k8s_drain` needs `force` on control-plane nodes (static-pod guard Pods); the NMState webhook stamps every NNCP
  update, so apply a policy only when its spec differs.

## Layout
```
ansible.cfg, inventory/     relative paths; localhost only
docker/, compose.yaml       toolbox image (route A) and its pinned requirements (also route B's)
clusters/<name>.yaml        the migration config (gitignored); cluster.example.yaml is the tracked example
playbooks/                  migrate (tags = phases), demo, verify, tools/{attach_check,show_breakpoint,check_prepared,roll_selftest,array_hosts}
roles/nvme_model            config -> validated model (mig_model filter), kubeconfig and token
roles/nvme_preflight        discover (live objects, stage) and checks (the gate)
roles/nvme_probe            privileged probe Pod per node -> probe_facts
roles/nvme_array            purefa_info wrappers
roles/nvme_hostprep         M2 MachineConfig, the roll with per-node checks (files/: the hostnqn unit)
roles/nvme_migrate          enable, node, verify_node (attach check: test Pods -> mig_evidence), evidence (the end-of-phase report), finalize, rollback(_node), report, show_breakpoint (defaults: the operator's texts, mig_attach_pods)
roles/nvme_demo             demo StatefulSets: deploy, verify, bounce, teardown
plugins/filter/             nvme_migration.py (model and helpers), nvme_k8s.py (object readers, NMState)
tests/model_test.py         offline tests
tools/sync.sh               optional copy to a jump box
.claude/skills/             the maintainers' procedures (see "Skills" below)
.state/                     gitignored: kubeconfig, fa-token, per-cluster snapshots (.state/<name>/), logs
```

## Checks before declaring a change done
- `python3 tests/model_test.py` (route A: `docker compose run --rm -T toolbox python3 tests/model_test.py`)
- `ansible-playbook <each playbook> --syntax-check -e cluster=<name>`
- `--check` of every phase against the live cluster; for a phase change run it twice, the second must be `changed=0`.

## Skills (`.claude/skills/`, for whoever maintains the project; none is needed to use the tool)
| Skill | Use it for |
| --- | --- |
| `verify-change` | The checks above, in full, and what to look at so the words stay true. Last step of every change. |
| `run-phase` | Running a phase on a live cluster: the plan, the user's go-ahead, `confirm`, a terminal, the proof afterwards. |
| `diagnose-run` | A failed or interrupted run: read-only look, symptom table, and where to record a new finding. |
| `change-model` | A new or changed config field: where the value should live, and the touch list. |
| `change-roles` | Editing roles, playbooks or filters: the hard rules, check mode, the measured traps, the hostnqn unit. |
| `update-versions` | Dependency pins, a new PX-CSI, OpenShift or Purity release, and the "Tested with" claim. |
| `publish` | Scan, commit and push to the public repository, and keep the disclaimer and trademark text intact. |

A skill carries no site value (no cluster, host or array name, no address): they are published with the repository.
