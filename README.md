# PX-CSI iSCSI to NVMe/TCP migration

Moves the volumes of a running Red Hat® OpenShift® cluster's Portworx® CSI (PX-CSI) from iSCSI to NVMe®/TCP on the same
Everpure FlashArray™ product, one node at a time, with PX-CSI's multi-SAN protocol feature (`StorageCluster.spec.nodes[].env`,
PX-CSI 26.3.0 and later). It is an Ansible® project that talks only to the Kubernetes® API and to the array's management API (the
`kubernetes.core` and `everpure.flasharray` collections), changes nothing on a node by hand, and only **reads** the array.

## Disclaimer
**This is an independent, unofficial tool. It is not provided, endorsed, certified or supported by Everpure, Inc. or any of its
affiliates**, and nothing in this project is an Everpure product, service or deliverable. Any support that Everpure provides for
its own products does not extend to this tool.

**You use it entirely at your own risk.** It operates on a running production cluster: it drains and reboots nodes, applies
MachineConfigs and network policies, restarts the PX-CSI node plugins and changes the StorageCluster configuration, and it
depends on a person changing the network. A defect, a mistake or an unforeseen condition can cause application downtime, loss of
access to storage, or data loss. You are solely responsible for reviewing the code, testing it outside production, having
backups and a maintenance window, and for everything that results from running it.

**Everpure, Inc., its affiliates, and their respective officers, directors, employees and agents have no responsibility or
liability of any kind for this tool or its use, and shall not be held responsible in any manner** for any direct, indirect,
incidental, special, consequential or exemplary damages or losses (including loss of data, profits or business, or interruption
of service) arising out of or in connection with the tool, whatever the legal theory and even if advised of the possibility of
such damage. The same applies to the authors and contributors of this project. The software is provided "AS IS", without
warranty of any kind (see sections 7 and 8 of the [Apache License 2.0](LICENSE)); where the law does not allow a disclaimer or
a limitation, it applies to the fullest extent the law permits.

Nothing in this project creates any obligation for Everpure or changes the terms of any agreement, warranty or support
entitlement you have with Everpure.

**Trademarks.** Everpure, the Everpure P Logo, Pure Storage, and the marks on the Everpure Trademark List
([everpuredata.com/trademarks](https://www.everpuredata.com/trademarks)), which include FlashArray™, ActiveCluster™ and
Portworx®, are trademarks or registered trademarks of Everpure, Inc. in the U.S. and/or other countries. Red Hat®, OpenShift®
and Ansible® are trademarks of Red Hat, LLC, registered in the United States and other countries. Kubernetes® is a registered
trademark of The Linux Foundation in the United States and other countries. NVM Express® and NVMe® are registered trademarks
of NVM Express, Inc. Docker and the Docker logo are trademarks or registered trademarks of Docker, Inc. in the United States
and/or other countries. Other names may be trademarks of their respective owners. All of them are used here only to identify
the products this tool works with, and their use does not imply any affiliation with, or endorsement by, their owners.

## Contents
- [How it works](#how-it-works)
- [Before you start](#before-you-start)
- [What you give it](#what-you-give-it)
- [Set up the machine that runs it](#set-up-the-machine-that-runs-it)
- [Run the migration](#run-the-migration)
- [At the breakpoint](#at-the-breakpoint)
- [What it changes on your cluster](#what-it-changes-on-your-cluster)
- [How it keeps the migration safe](#how-it-keeps-the-migration-safe)
- [If something goes wrong](#if-something-goes-wrong)
- [Rolling back](#rolling-back)
- [After the migration](#after-the-migration)
- [Tested with](#tested-with)
- [Reference](#reference)
- [License](#license)

## How it works
The cluster starts on iSCSI. The NVMe/TCP portals of the array are normally on **other networks** than the iSCSI portals, and
the nodes have one NIC per storage network, so each node's storage NICs have to move to the NVMe/TCP networks. That is a change
outside the cluster (a VLAN change on the node's switch ports, a port group, ...), so the tool stops at a **breakpoint** and
waits for a person to make it, then checks the result instead of trusting the keypress.

| Phase | What happens |
| --- | --- |
| `preflight` | Read-only gate: the cluster, PX-CSI, the nodes' NICs and the array's NVMe/TCP portals must be as the config says. |
| `prep` | Makes every storage node ready for NVMe/TCP while it keeps serving iSCSI (one MachineConfig, a rolling reboot, each node checked as soon as it is back). |
| `enable` | Adds one entry to `StorageCluster.spec.nodes`: nodes that carry the migration label will use NVMe/TCP. Nothing restarts. |
| `nodes` | One node at a time: cordon and drain, wait until PX has nothing attached to it, **breakpoint** (the person changes the network), set the new addresses (NMState), check that every array portal answers on port 4420, label the node, restart its node plugin, check that PX reports `NVMEOF-TCP`, uncordon, then prove it with test Pods and fresh volumes. |
| `finalize` | Switches the whole StorageCluster to NVMe/TCP, removes the node override and the labels, checks every node again. |
| `rollback` | The way back, node by node (see [Rolling back](#rolling-back)). |

While `nodes` runs the cluster is mixed: nodes on both protocols, and existing volumes follow their pods across nodes of either
protocol. Each node is drained once and every storage node is rebooted once (by `prep`), so plan it like rolling node
maintenance: pods on a node are evicted when it is drained, and stateful pods restart on other nodes.

Measured on a three-node control plane that also runs the workloads: `prep` about 24 minutes for the three nodes, about 7
minutes of automated work per node in `nodes` plus the time the person needs at the breakpoint, `finalize` about 6 minutes.

## Before you start
**Cluster**
- OpenShift on RHCOS, with the MachineConfigPool(s) of the storage nodes (`master` on a compact cluster, else `worker`).
- PX-CSI **26.3.0 or later** in CSI mode (FlashArray Direct Access), StorageCluster `Running`, the whole cluster on iSCSI.
- The **kubernetes-nmstate operator** installed (each node needs a `NodeNetworkState`). The tool does not install it.
- Nodes `Ready` and the cluster operators healthy: the preflight refuses to start otherwise.

**Array**
- One FlashArray with an **enabled NVMe/TCP interface on every target network** (the preflight stops if one is missing).
- An array-level account with the **Storage Admin** role, not one scoped to a realm (see [What you give it](#what-you-give-it)).

**Network**
- Each storage node has one NIC per storage network, identified by its **MAC address**. The tool moves the network behind the
  NICs, never the NICs, and never asks anyone to change an address on a node: it sets them through NMState.
- The target addresses must be free on the new networks (you confirm at the breakpoint), and the new networks must reach the
  array's NVMe/TCP portals.
- Someone who can make the network change (switch ports, hypervisor port groups) is at the keyboard during `nodes`.

**The machine that runs the tool**: Docker, or an Ansible machine with the pinned versions below; it reaches the cluster's API and
the array's management address; and it has a **terminal** for the `nodes` phase (the breakpoint is an interactive prompt).

**Not covered**: the ActiveCluster™ solution over NVMe/TCP (PX-CSI does not support it yet), NVMe multi-interface (PX-CSI 26.4.0 and later),
NVMe/RDMA and FC, more than one FlashArray per cluster, Portworx Enterprise (non-CSI mode), IPv6.

## What you give it
1. **`clusters/<name>.yaml`**, from [cluster.example.yaml](cluster.example.yaml) (a file under `clusters/` is never committed). The
   name in it is what you pass as `-e cluster=<name>`. It holds the storage nodes (Kubernetes node name and, per storage NIC,
   its MAC, its address now on the iSCSI network and its address after on the NVMe/TCP network), the two lists of networks
   (`current` and `target`, NIC n moves from `current[n]` to `target[n]`), the array's management address, the Portworx
   namespace and StorageClass, the MachineConfigPools, the node label, and free-text notes for the person at the breakpoint.
   **Fill in the starting state before the first node moves.** The config is the initial inventory, and a rollback is only as
   good as it: the tool refuses to load without the current networks, each NIC's MAC and current address (the preflight compares
   them with the live nodes), and `migration.operator_note_rollback`, the physical network the NICs leave and who puts it back,
   which nothing else records.
2. **A kubeconfig** with cluster-admin rights, at the path `cluster.kubeconfig` names (default `.state/kubeconfig`; a relative path
   is relative to the project directory).
3. **An array account.** The array's management address goes in the config (`flasharray.endpoint`). The API token of an array-level
   account with the **Storage Admin** role (the tool only reads: it needs to list network interfaces, hosts and pods) goes in the
   environment variable `FA_API_TOKEN` (never on a command line; the Docker route
   forwards it into the container) or in the file `.state/fa-token` (mode 0600; it may be an Ansible Vault file, then set
   `ANSIBLE_VAULT_PASSWORD_FILE`). It is never written in the YAML. PX-CSI's own token is not used: it may be scoped to its realm
   and then cannot list the array's network interfaces, which the preflight reads to find the NVMe/TCP portals.

Nothing else about the array is configured, so nothing can disagree with what PX-CSI uses. The tool reads the `px-pure-secret` in
the Portworx namespace (`portworx.namespace`, the setting that must be right) and **stops if it does not list the endpoint you
gave**, so the config cannot be about another array than the one PX-CSI uses. The realm comes from that secret and the array pod
from the StorageClass (`pure_fa_pod_name`); a `pod` or `realm` in the config is refused. The run prints which array it found.

## Set up the machine that runs it
Both routes run the same files with the same commands after `ansible-playbook`, with the same `ansible.cfg` (paths relative to
itself). Nothing is installed on a jump box other than what the route below says.

**A. Docker** (a jump box, or any Docker host). The project builds its own toolbox image from `docker/` (ansible-core, the two
collections and their Python libraries, pinned), so nothing is installed on the host.
```bash
cd ocp-px-csi-iscsi2nvmetcp                              # copy the project here (git clone, scp, or tools/sync.sh)
mkdir -p .state && cp <your kubeconfig> .state/kubeconfig
docker compose build                                     # once: builds the toolbox image
mig() { docker compose run --rm -T toolbox "$@"; }       # a shell function, once per session
mig python3 tests/model_test.py
mig ansible-playbook playbooks/migrate.yml -e cluster=<name> --tags preflight
```
`nodes` needs a terminal: leave out `-T` (`docker compose run --rm toolbox ansible-playbook …`) and, from a workstation, connect
with `ssh -t`. Long runs such as `prep` go in the background with `-T` and the output in a log under `.state/`.

**B. A plain Ansible machine.** Install the same pins the image uses:
```bash
pip install -r docker/requirements.txt                   # ansible-core, kubernetes, py-pure-client
ansible-galaxy collection install -r docker/requirements.yml
export FA_API_TOKEN=...                                  # or .state/fa-token
python3 tests/model_test.py
ansible-playbook playbooks/migrate.yml -e cluster=<name> --tags preflight
```
Pins: ansible-core 2.21.4, kubernetes 36.0.3, py-pure-client 1.94.2, `kubernetes.core` 6.5.0, `everpure.flasharray` 1.45.1.

## Run the migration
In route A prefix the commands with `mig` (and drop `-T` for `nodes`); in route B run them directly. Always pass
`-e cluster=<name>`. **Without `--tags` only the read-only preflight runs.**

| Step | Command (`ansible-playbook playbooks/…`) | Changes | Needs |
| --- | --- | --- | --- |
| 1. Gate | `migrate.yml --tags preflight` | nothing | |
| 2. Baseline workloads (recommended) | `demo.yml` | a namespace with two StatefulSets and PVCs: the workloads you can watch survive the migration | |
| 3. Prepare the nodes | `migrate.yml --tags prep` | one MachineConfig, **rolling reboot of every storage node** | `-e confirm=<name>` |
| 4. Enable the override | `migrate.yml --tags enable` | `spec.nodes` of the StorageCluster (no restart) | |
| 5. Preview the operator's texts | `tools/show_breakpoint.yml` | nothing: renders what the person will read, for every node | |
| 6. Migrate the nodes | `migrate.yml --tags nodes` (interactive) | drains each node and waits for the person; ends with test Pods on each node and an evidence report | `-e confirm=<name>` |
| 7. Finish | `migrate.yml --tags finalize` | StorageCluster env, labels; **every node plugin restarts** | `-e confirm=<name>` |
| 8. Prove it | `demo.yml --tags bounce,verify`, `verify.yml` | restarts the demo pods so every volume is re-attached over NVMe/TCP; `verify.yml` is read-only | |

Rules the tool enforces:
- **Run every phase with `--check` first** (a dry run: it prints the plan and creates nothing). `prep`, `nodes`, `finalize` and
  `rollback` refuse to run without `-e confirm=<name>`, so adding it is a decision, not a habit.
- **`nodes` asks "start?" before it drains anything.** Without a terminal it stops there with the node untouched. Limit it to some
  nodes with `-e migrate_nodes=<node>` (or `-e 'migrate_nodes=["a","b"]'`). A node that is done is skipped, and a node interrupted
  after the network change carries on without asking again.
- A second run of `prep`, `enable`, `nodes` or `finalize` reports `changed=0`.
- To read an interactive run afterwards, keep a log: route A adds `-e ANSIBLE_LOG_PATH=/work/.state/<name>-nodes.log` to
  `docker compose run`; route B sets `ANSIBLE_LOG_PATH=.state/<name>-nodes.log`.

Tools: `tools/attach_check.yml` (the attach check and its evidence report on any node, on demand), `tools/check_prepared.yml`
(the per-node preparation check), `tools/array_hosts.yml` (the cluster's host objects on the array), `tools/roll_selftest.yml`
(exercises the `prep` steps without rebooting anything).

## At the breakpoint
When a node is cordoned, drained and has nothing PX attached, the tool prints what the person has to do: for each NIC (by MAC) the
old and the new network (name, CIDR), the address the playbook will set, and your `operator_note` (the site's own words: which
ports, which VLAN ids, who to ask). The person changes **only the network**, never an address on the node, makes sure the new
addresses are free there, and presses Enter. The tool then applies the addresses with NMState, waits until the node reports them,
and checks that every array NVMe/TCP portal answers on port 4420; if one does not, it stops and nothing else changes.

**Change the network only when the breakpoint asks.** Moving it earlier cuts the iSCSI paths of volumes that are still attached,
and they can then no longer be detached (see [If something goes wrong](#if-something-goes-wrong)).

Run `tools/show_breakpoint.yml` before `nodes`: a text that failed to render would otherwise be found with a node drained.

## What it changes on your cluster
| Phase | Objects |
| --- | --- |
| `prep` | MachineConfig `99-<pool>-pure-nvme-prep` per pool: `nvme_tcp` loaded at boot, a unique NVMe host NQN per node, the `queue-depth` I/O policy rule and native multipath (or, if your nodes run DM multipath, the Pure NVMe stanza as a drop-in). It only adds; whatever configures iSCSI stays. |
| `enable` | One entry in `StorageCluster.spec.nodes`: nodes labelled `storage-protocol=nvme-tcp` (configurable) get `PURE_FLASHARRAY_SAN_TYPE=NVMEOF-TCP`. |
| `nodes` | Per node: its `NodeNetworkConfigurationPolicy` (name from `nmstate.policy`, default `storage-<node>`: updated if it exists, created if not) with the target addresses; the node label; its node plugin Pod (deleted, so it restarts); temporary test Pods and PVCs; probe Pods in the namespace `ocp-px-migration-checks`. |
| `finalize` | `StorageCluster.spec.env`: the SAN type set to `NVMEOF-TCP` and the iSCSI settings (`PURE_ISCSI_*`, `ENABLE_SUBNET_AWARE_ISCSI_LOGIN`) removed, nothing else touched; the node override and the labels removed. `PURE_NVME_ALLOWED_CIDRS` is added only if the array has NVMe/TCP portals outside the target networks. |
| `demo.yml` | A namespace (`migration.demo.namespace`, default `px-nvme-demo`) with the demo workloads. |
| the array | Nothing is written. PX-CSI creates and removes its host objects and volumes as it always does. |
| the machine running the tool | `.state/<name>/`: the starting state kept for a rollback (see below). |

## How it keeps the migration safe
- **Preflight gate** before every phase, never repairing, only reporting: nodes Ready, cluster operators and PX-CSI healthy, the
  PX-CSI version supports multi-SAN, one node plugin Pod per storage node, every configured NIC exists on its node by MAC and
  carries its current (or its target) address, and every target network has an enabled NVMe/TCP portal on the array. When the
  config is loaded, every phase also checks that the `px-pure-secret` lists your array.
- **Nothing moves while a volume is attached.** PX-CSI requires a node to be drained first: a host registered with NVMe
  credentials cannot connect to a volume that was attached over iSCSI. The tool waits until there is no `VolumeAttachment` for
  the node, no volume on the node's array host and no iSCSI LUN, and stops with the node drained if that does not happen.
- **Enter is not trusted.** After the breakpoint the tool checks the node's addresses and every portal, and after the plugin restart
  that PX-CSI reports NVMe/TCP for the node.
- **Every node ends with proof.** Two test Pods with fresh volumes are pinned to the node, and the run asserts and prints, per
  node: each Pod's `/data` is on an `nvme*` device and its checksum reads back, the node has NVMe controllers over `tcp` to every
  array portal, the I/O policy is `queue-depth`, and the array host found by the node's NQN holds a connection to every test
  volume. The evidence of all nodes of the run is printed again at the end of `nodes` and `finalize`.
- **Secrets stay in memory or in a file you control**: never on a command line, never in the YAML, and the tasks that carry the
  token do not log it.
- **The array is only read** (`network`, `hosts` and `pods` information). PX-CSI is the only writer.

## If something goes wrong
Re-running a phase is safe: it resumes. These are the cases to know:

| You see | Cause and what to do |
| --- | --- |
| `nodes` stops at "no PX volume attached to the node" and the node stays drained | The network behind the node's storage NICs was changed before the breakpoint asked, so the iSCSI paths are down and PX-CSI cannot unmount the volumes. Put the network back (that is the fix; the next kubelet retry detaches the volumes within a couple of minutes), then run `nodes` again. |
| The preflight refuses to start: operators `authentication`, `openshift-apiserver`, `machine-config` unhealthy | A node was left cordoned for about 30 minutes by a failed or interrupted run: on a three-node control plane some API server replicas cannot be scheduled. Uncordon the node (`oc adm uncordon <node>`), wait about a minute until the operators settle, and run `nodes` again (it drains the node again). |
| Warning `timeout reached while pods were still running` during the drain | Harmless, and the run continues. On a control plane the operators recreate finished pruner Pods under the same name, which the drain counts as still running. The next step, the volume wait, is the real check. Raise the wait with `-e mig_drain_wait=900` only for workloads with a long termination grace period (default 120 s). |
| Warning `cannot delete mirror Pods using API server` | Expected for static Pods on a control-plane node. |
| `stdin is not a terminal` at "start?" | `nodes` needs a terminal. Nothing was changed. Run it interactively (`ssh -t`, no `-T`). |
| The run stops: the secret does not list your endpoint | `flasharray.endpoint` must be written as the `px-pure-secret` writes it. |
| The array refuses to read `network` | The token must be that of an array-level account (Storage Admin), not one scoped to a realm. |
| `prep` paused the MachineConfigPools | A node came back wrong, so the tool paused the pools before the next reboot. Fix the node, run `--tags prep -e confirm=<name>` again: it unpauses what it paused and carries on. |
| Four iSCSI sessions remain on a node that has no iSCSI LUN | Leftovers on the old network, harmless. Do not clear them by hand. |

## Rolling back
`migrate.yml --tags rollback -e confirm=<name>` (add `-e 'rollback_nodes=["<node>"]'` to limit it) puts the StorageCluster back
to the state `enable` recorded, then walks every node that is on NVMe/TCP back: drain, wait for no volume, a **breakpoint** where
the person moves the network back, NMState with the recorded addresses, label removed, node plugin restarted, uncordon, and an
attach check over iSCSI. Run its `--check` first.

What makes the way back possible is under `.state/<name>/` on the machine that runs the tool (never committed, so **keep it, with
the config, until the migration is closed**): `storagecluster-before.yaml` (written by `enable`) and `before/<node>.yaml`
(written by `nodes` before a node's network is touched: the storage NICs' interfaces and addresses, and the node's NMState policy
as they were; never overwritten). `nodes` refuses to move a node whose network it could not record. A rollback restores the
recorded policy; a node with none gets its policy rebuilt from `networks.current` (static IPv4 on each storage NIC).

`rollback` has been tested offline and with `--check`; it has not yet been run against a live cluster. Try it on one node of a
non-critical cluster before you depend on it.

## After the migration
- **Update whatever else describes the nodes' storage networks** to the target networks and addresses: infrastructure-as-code
  state, inventories, NMState sources in git, an IPAM or CMDB, switch documentation. The network change was made outside Ansible,
  and anything that re-applies the old values would put NICs, NMState policies or the StorageCluster env back to iSCSI.
- The MachineConfig `99-<pool>-pure-nvme-prep` stays: it is what keeps NVMe/TCP ready on every boot. The iSCSI configuration of
  the nodes is left alone; removing it is a separate change with its own rolling reboot, which this tool does not do.
- Remove the demo workloads when you no longer need them: `demo.yml --tags teardown -e confirm=<demo namespace>`. The namespace
  `ocp-px-migration-checks` (probe and test Pods) can be deleted too.
- Nothing is needed from the array side: PX-CSI creates a node's host object (now identified by the node's NQN) at its first attach
  and removes it after the last detach, as it always does.

## Tested with
The forward path (`preflight`, `prep`, `enable`, `nodes` on all nodes, `finalize`, the demo bounce) has been run end to end on a
live cluster: OpenShift 4.22 (RHCOS), a three-node control plane that also runs the workloads, PX-CSI driver 26.3.0 with operator
26.5.0, one FlashArray on Purity//FA 6.12, with native NVMe multipath. Not exercised live: `rollback`, and DM multipath mode (it
is implemented and asserted by the tests). `python3 tests/model_test.py` runs the model's validation and every helper offline,
without a cluster.

## Reference
```
playbooks/migrate.yml        the phases (tags: preflight prep enable nodes finalize rollback)
playbooks/demo.yml           demo workloads (tags: deploy bounce verify teardown)
playbooks/verify.yml         read-only report of where the migration stands
playbooks/tools/             attach_check, show_breakpoint, check_prepared, array_hosts, roll_selftest
clusters/<name>.yaml         your config (from cluster.example.yaml)
docker/, compose.yaml        the toolbox image and its pinned requirements
tests/model_test.py          offline tests
.state/                      kubeconfig, token file, per-cluster snapshots, logs (never committed)
```
For maintainers: [CLAUDE.md](CLAUDE.md) holds the project's rules and the gotchas found during the live runs.

## License
Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE), and the [Disclaimer](#disclaimer) at the top of this page.
