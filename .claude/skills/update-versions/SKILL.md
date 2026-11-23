---
name: update-versions
description: Bump or re-evaluate what this project depends on and is tested with. The pinned ansible-core, kubernetes and py-pure-client libraries, the kubernetes.core and everpure.flasharray collections, the Fedora toolbox base, and the PX-CSI, OpenShift and Purity//FA versions behind the README's "Tested with" and the gotchas. Use for dependency updates, a new PX-CSI release (especially 26.4 and NVMe multi-interface), a new OpenShift or Purity release, a security advisory on a pin, or any change to a version claim.
---

# Updating versions

## Where a version lives
| What | File |
| --- | --- |
| ansible-core, kubernetes, py-pure-client | `docker/requirements.txt` (exact `==`) |
| kubernetes.core, everpure.flasharray | `docker/requirements.yml` (exact `version:`) |
| Toolbox base image | `docker/Dockerfile`, `ARG FEDORA_IMAGE` |
| Image tag | `compose.yaml`, `image: px-nvme-migration-toolbox:<n>`: bump it when the image's contents change, so the old image is not reused |
| The pins as text | README, "Set up the machine that runs it" |
| What was run live | README, "Tested with" |
| Minimum PX-CSI | `roles/nvme_preflight/tasks/checks.yml` (`image_version_ge('26.3.0')`) and its case in `tests/model_test.py` |

Pins stay exact, and a new dependency is a new pinned entry there: nothing is installed on a jump box.

## Bumping a dependency (one at a time, so a break names its cause)
1. Read its changelog. For `everpure.flasharray`: do `purefa_info` `network`, `hosts` and `pods` still return the keys the
   filters read (`nvme_portals`, `fa_host_*`)? For `kubernetes.core`: `k8s_drain` (`force`, `wait_timeout`, delete options),
   `k8s_info`, `k8s` and its `merge_type`, `k8s_exec`. For ansible-core: anything that touches Jinja tests and filters, or
   `pause`, which the breakpoint depends on.
2. Edit the pin, bump the compose image tag, `docker compose build`.
3. `verify-change` on **both routes** (route B in a fresh virtual environment).
4. Live, read-only: `preflight` and `--check` of every phase on a cluster. What only a real `nodes` run exercises (drain,
   the breakpoint's `pause`, the plugin restart) is **not proven** by that: say so, and let the next real node migration be
   the evidence.
5. Update the README's pins line. Change "Tested with" only for what was actually run live, never for what is expected to work.

## A new PX-CSI, OpenShift or Purity release
- Read the official documentation first (docs.portworx.com: release notes, the multi-SAN, subnet-aware and NVMe pages; Pure's
  NVMe/TCP guides), and cite public pages only in tracked files. Internal runbooks may inform the work but are not linked from a
  published repo.
- Walk the CLAUDE.md gotchas and mark each as still true, fixed, or unknown. Do not delete a gotcha without a run that shows it is
  gone. The ones that depend on PX-CSI behavior: the node plugin caching `nvme_core.multipath`, `spec.nodes[].env` not restarting
  the plugin, `PURE_NVME_ALLOWED_IFACES` crashing a plugin on a non-NVMe node, PX creating a node's array host on first attach and
  removing it after the last detach.
- OpenShift: the MachineConfig and Ignition version in the file builder, the `nmstate.io` API versions, the MachineConfigPool
  behavior `prep` depends on. Purity: `purefa_info` fields.
- Run the preflight on the new version first. Raise the minimum in the preflight only when the tool truly needs it.

## Version scope
The tool targets the GA PX-CSI the cluster runs (>= 26.3.0, multi-SAN). **NVMe multi-interface needs >= 26.4.0 and is out of
scope** until it is GA and the owner decides to take it on. What it would involve:
- `PURE_NVME_ALLOWED_IFACES` (NIC names from NodeNetworkState by MAC) and `ENABLE_SUBNET_AWARE_NVME_LOGIN=true`.
- They may only sit in the per-node override until `finalize`, because they crash a node plugin whose SAN type is not
  `NVMEOF-TCP`.
- Enable them in the same window as the SAN type switch: 26.4 never disconnects NVMe controllers, so enabling later leaves the
  old single-path controllers until a reboot.
- Without multi-interface PX makes one connection per array NVMe/TCP portal and the kernel picks the NIC; with the NICs on
  separate subnets each portal is reached through its own NIC. `PURE_NVME_ALLOWED_CIDRS` filters the array's portals only.
