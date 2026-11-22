"""Pure helpers for the iSCSI -> NVMe/TCP migration playbooks. No cluster or array access: unit-tested by
tests/model_test.py. Nothing here depends on any other project."""
import base64
import ipaddress
import json
import re

from ansible.errors import AnsibleFilterError

SAN_TYPE = "PURE_FLASHARRAY_SAN_TYPE"
ISCSI_ENV = re.compile(r"^(PURE_ISCSI_.*|ENABLE_SUBNET_AWARE_ISCSI_LOGIN)$")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


# --- the migration model -------------------------------------------------------------------------------------------
def _die(msg):
    raise AnsibleFilterError(msg)


def _networks(raw, key):
    """networks.<key> -> [{name, cidr, prefix, mtu, net}], validated."""
    out, seen = [], set()
    for n in ((raw.get("networks") or {}).get(key) or []):
        for field in ("name", "cidr"):
            if not n.get(field):
                _die(f"networks.{key} entry {n}: missing '{field}'")
        if n["name"] in seen:
            _die(f"networks.{key}: '{n['name']}' defined twice")
        seen.add(n["name"])
        try:
            net = ipaddress.ip_network(str(n["cidr"]), strict=True)
        except ValueError as exc:
            _die(f"networks.{key} '{n['name']}': {exc}")
        try:
            mtu = int(n.get("mtu", 1500))
        except (TypeError, ValueError):
            _die(f"networks.{key} '{n['name']}': mtu must be a number")
        out.append({"name": n["name"], "cidr": str(net), "prefix": net.prefixlen, "mtu": mtu, "net": net})
    return out


def mig_model(raw):
    """The migration config (clusters/<name>.yaml) -> the model the playbooks use, or an error naming what is wrong.

    The migration moves the network behind each node's storage NICs (a VLAN change on the switch ports), never the NICs:
    a NIC is identified by its MAC, and has one address on its iSCSI network now and one on its NVMe/TCP network after.
    No platform detail is part of this model."""
    raw = raw or {}
    cl = raw.get("cluster") or {}
    if not cl.get("name"):
        _die("cluster.name is required")
    fa = raw.get("flasharray") or {}
    if not fa.get("endpoint"):
        _die("flasharray.endpoint is required (the array's management address, as the px-pure-secret writes it)")
    if "pod" in fa or "realm" in fa:
        _die("flasharray.pod and flasharray.realm: remove them. The realm is read from the px-pure-secret and the pod from the "
             "StorageClass, so a copy here could only disagree with them.")
    px = raw.get("portworx") or {}
    pools = raw.get("machineconfig_pools") or []
    if not pools or not all(isinstance(x, str) and x for x in pools):
        _die("machineconfig_pools is required: the MachineConfigPool names of the storage nodes, e.g. [master] or [worker]")
    policy = str((raw.get("nmstate") or {}).get("policy") or "storage-{node}")
    if "{node}" not in policy:
        _die("nmstate.policy must contain {node}: the name of each node's NodeNetworkConfigurationPolicy, e.g. storage-{node}")
    mg = raw.get("migration") or {}
    label = str(mg.get("label") or "storage-protocol=nvme-tcp")
    if "=" not in label or not label.split("=", 1)[0] or not label.split("=", 1)[1]:
        _die("migration.label must be key=value")
    if str(mg.get("multipath", "auto")) not in ("auto", "Y", "N"):
        _die("migration.multipath must be auto, Y or N")
    if not str(mg.get("operator_note_rollback") or "").strip():
        _die("migration.operator_note_rollback is required: write down the physical network the storage NICs are on today "
             "(switch ports, VLAN ids or port groups) and who puts it back. Nothing else records it, and a rollback needs "
             "it. Fill it in before the first node moves.")

    current, target = _networks(raw, "current"), _networks(raw, "target")
    if not current or len(current) != len(target):
        _die("networks.current and networks.target need the same number (at least one) of storage networks: "
             "NIC n moves from current[n] to target[n]")
    nodes_raw = raw.get("nodes") or []
    if not nodes_raw:
        _die("nodes is required: the nodes that carry storage (every PX-CSI node)")

    names, hosts, macs, cur_ips, new_ips, nodes = set(), set(), set(), set(), set(), []
    for n in nodes_raw:
        name = n.get("name")
        if not name:
            _die(f"node {n}: missing 'name'")
        hostname = n.get("hostname") or name
        if name in names or hostname in hosts:
            _die(f"node '{name}' or hostname '{hostname}' defined twice")
        names.add(name)
        hosts.add(hostname)
        nics = n.get("nics") or []
        if len(nics) != len(current):
            _die(f"{name}: {len(nics)} nics, but {len(current)} storage networks: one entry per network, in order")
        cur_storage, new_storage = [], []
        for i, nic in enumerate(nics):
            mac = str(nic.get("mac") or "").lower()
            if not MAC_RE.match(mac):
                _die(f"{name} nic {i + 1}: mac '{nic.get('mac')}' is not aa:bb:cc:dd:ee:ff")
            if mac in macs:
                _die(f"{name} nic {i + 1}: mac {mac} used twice")
            macs.add(mac)
            for key, nets, seen, store in (("address", current, cur_ips, cur_storage),
                                           ("target_address", target, new_ips, new_storage)):
                try:
                    ip = ipaddress.ip_address(str(nic.get(key)))
                except ValueError:
                    _die(f"{name} nic {i + 1}: {key} '{nic.get(key)}' is not an IP address")
                net = nets[i]
                if ip not in net["net"] or ip in (net["net"].network_address, net["net"].broadcast_address):
                    _die(f"{name} nic {i + 1}: {key} {ip} is not a usable host of {net['name']} {net['cidr']}")
                if str(ip) in seen:
                    _die(f"{name} nic {i + 1}: {key} {ip} is used by another NIC")
                seen.add(str(ip))
                store.append({"name": net["name"], "mac": mac, "address": str(ip), "prefix": net["prefix"], "mtu": net["mtu"]})
        nodes.append({
            "name": name,
            "hostname": hostname,
            "current_storage": cur_storage,
            "target_storage": new_storage,
            "nics": [{"mac": cur["mac"],
                      "old": dict(cur, cidr=current[i]["cidr"]),
                      "new": dict(tgt, cidr=target[i]["cidr"])}
                     for i, (cur, tgt) in enumerate(zip(cur_storage, new_storage))],
        })

    key, value = label.split("=", 1)
    return {
        "cluster": {"name": str(cl["name"]), "kubeconfig": str(cl.get("kubeconfig") or ".state/kubeconfig")},
        "flasharray": {"endpoint": str(fa["endpoint"]), "token_env": str(fa.get("token_env") or "FA_API_TOKEN"),
                       "token_file": str(fa.get("token_file") or ".state/fa-token")},
        "portworx": {"namespace": str(px.get("namespace") or "portworx"), "storageclass": str(px.get("storageclass") or "px-fa")},
        "mc_pools": [str(x) for x in pools],
        "nmstate_policy": policy,
        "label": {"key": key, "value": value},
        "multipath": str(mg.get("multipath", "auto")),
        "demo_namespace": str((mg.get("demo") or {}).get("namespace") or "px-nvme-demo"),
        "operator_note": str(mg.get("operator_note") or "").strip(),
        "operator_note_rollback": str(mg.get("operator_note_rollback") or "").strip(),
        "cidrs": [n["cidr"] for n in target],
        "nodes": nodes,
    }


NIC_KEYS = ("name", "type", "state", "mtu", "mac-address", "ipv4", "ipv6")


def mig_before_snapshot(node, nns, nncp, taken=""):
    """What one node's storage network is before its migration, to keep for a rollback: the interface of each storage NIC
    (found by MAC in the node's NodeNetworkState: name, type, state, MTU, addresses) and the node's storage
    NodeNetworkConfigurationPolicy as it is (name, labels, spec), or None when the node has none yet."""
    ifaces = (((nns or {}).get("status") or {}).get("currentState") or {}).get("interfaces") or []
    by_mac = {str(i.get("mac-address", "")).lower(): i for i in ifaces if i.get("mac-address")}
    nics = []
    for nic in node["nics"]:
        iface = by_mac.get(nic["mac"])
        if iface is None:
            _die(f"{node['name']}: no interface with MAC {nic['mac']} in the node's NodeNetworkState: nothing to record")
        nics.append({"mac": nic["mac"], "network": nic["old"]["name"],
                     "interface": {k: iface[k] for k in NIC_KEYS if k in iface}})
    policy = None
    if nncp:
        md = nncp.get("metadata") or {}
        policy = {"name": md.get("name"), "labels": dict(md.get("labels") or {}), "spec": nncp.get("spec") or {}}
    return {"node": node["name"], "hostname": node["hostname"], "taken": str(taken), "policy": policy, "storage_nics": nics}


# --- the array PX-CSI itself uses: its secret and its StorageClass ----------------------------------------------------------
def fa_from_secret(secret, endpoint):
    """The px-pure-secret (a Secret resource, or None) and the array address the config names -> {error, realm, endpoints}.

    The config's array must be one of the FlashArrays of the secret's pure.json (compared as written, case apart): the array PX-CSI
    uses and the one this project reads must be the same. `realm` is that entry's realm ("" when it has none). `error` is empty
    when all is well, else says what is wrong. The API token the file holds is never returned nor put in an error."""
    out = {"error": "", "realm": "", "endpoints": []}

    def fail(msg):
        out["error"] = msg
        return out

    if not secret:
        return fail("there is no such secret")
    raw = (secret.get("data") or {}).get("pure.json")
    if not raw:
        return fail("it has no pure.json key")
    try:
        doc = json.loads(base64.b64decode(raw))
    except Exception:  # not echoed: the text would be the secret's content
        return fail("pure.json is not valid JSON")
    arrays = doc.get("FlashArrays") if isinstance(doc, dict) else None
    if not isinstance(arrays, list) or not arrays or not all(isinstance(a, dict) for a in arrays):
        return fail("pure.json has no FlashArrays list")
    out["endpoints"] = [str(a.get("MgmtEndPoint") or "").strip() for a in arrays]
    want = str(endpoint or "").strip().lower()
    mine = [a for a in arrays if str(a.get("MgmtEndPoint") or "").strip().lower() == want]
    if not want or not mine:
        return fail(f"pure.json lists {', '.join(out['endpoints'])}: none is {endpoint or '(no endpoint given)'}, the "
                    "flasharray.endpoint of the config; it must name an array PX-CSI uses, written the same way")
    out["realm"] = str(mine[0].get("Realm") or "").strip()
    return out


def fa_pod_path(storageclass, realm=""):
    """The array pod PX-CSI puts volumes in: the StorageClass's pure_fa_pod_name, qualified with the secret's realm when it is
    not already ("realm::pod"); empty when the StorageClass names no pod."""
    name = str(((storageclass or {}).get("parameters") or {}).get("pure_fa_pod_name") or "").strip()
    if not name or "::" in name or not realm:
        return name
    return f"{realm}::{name}"


# --- FlashArray (purefa_info results) ------------------------------------------------------------------------------
def nvme_portals(net_info, cidrs=None):
    """purefa_info `network` subset -> the array's NVMe/TCP portals [{interface, address, cidr}]; `cidr` is the target
    CIDR holding the address, or "" when it lies outside all of them."""
    nets = [ipaddress.ip_network(x) for x in cidrs or []]
    rows = []
    for name, info in sorted((net_info or {}).items()):
        if not isinstance(info, dict):
            continue
        if "nvme-tcp" not in [str(s).lower() for s in (info.get("services") or [])]:
            continue
        if info.get("enabled") is False:
            continue
        try:
            ip = ipaddress.ip_address(str(info.get("address") or ""))
        except ValueError:
            continue
        if ip.is_unspecified:
            continue
        rows.append({"interface": name, "address": str(ip),
                     "cidr": next((str(n) for n in nets if ip in n), "")})
    return rows


def _as_list(value):
    if value in (None, ""):
        return []
    return [value] if isinstance(value, str) else list(value)


def fa_host_with(hosts, key, value):
    """purefa_info `hosts` subset -> the first host whose list `key` ('nqn' or 'iqn') holds `value`: {name: ..., **host}.
    {} when there is none."""
    for name, host in sorted((hosts or {}).items()):
        if value and str(value).lower() in [str(x).lower() for x in _as_list((host or {}).get(key))]:
            return dict(host, name=name)
    return {}


def fa_host_connections(host):
    """Number of volumes connected to a host, or -1 when purefa_info doesn't report them."""
    if not host or "volumes" not in host:
        return -1
    vols = host["volumes"]
    return len(vols) if hasattr(vols, "__len__") else -1


def fa_host_volumes_for(host, pv_names):
    """Of the PV names (`pvc-<uid>`), those that have a volume connected to the array host. PX names the array volume
    `<realm>::<pod>::px_<id>-<pv name>`, so a volume matches when its name ends with `-<pv name>` (or equals it). The
    host's `volumes` entries are {volume: name, ...} (purefa_info) or plain names."""
    vols = []
    for v in (host or {}).get("volumes") or []:
        vols.append(str(v.get("volume", "") if isinstance(v, dict) else v))
    return [n for n in pv_names or [] if n and any(v == n or v.endswith("-" + n) for v in vols)]


# --- StorageCluster ------------------------------------------------------------------------------------------------
def px_env_set(env, name, value):
    """Env list with `name` set to `value`: replaced in place, or appended."""
    out, done = [], False
    for e in env or []:
        if e.get("name") == name:
            out.append({"name": name, "value": str(value)})
            done = True
        else:
            out.append(e)
    return out if done else out + [{"name": name, "value": str(value)}]


def px_env_get(env, name, default=""):
    return next((e.get("value", default) for e in env or [] if e.get("name") == name), default)


def px_env_drop_iscsi(env):
    """Env list without the iSCSI settings (PURE_ISCSI_*, ENABLE_SUBNET_AWARE_ISCSI_LOGIN). Nothing else is touched."""
    return [e for e in env or [] if not ISCSI_ENV.match(str(e.get("name", "")))]


def _selects(node_entry, key):
    match = (((node_entry or {}).get("selector") or {}).get("labelSelector") or {}).get("matchLabels") or {}
    return key in match


def px_nodes_entry(key, value, env):
    """spec.nodes[] entry: the nodes carrying label key=value get these env overrides."""
    return {"selector": {"labelSelector": {"matchLabels": {key: value}}}, "env": env}


def px_nodes_with(nodes, entry, key):
    """spec.nodes with our entry (the one selecting label `key`) added or replaced; other entries stay."""
    return [n for n in nodes or [] if not _selects(n, key)] + [entry]


def px_nodes_without(nodes, key):
    return [n for n in nodes or [] if not _selects(n, key)]


def px_node_override_ok(nodes, key, env):
    """Our override (the entry selecting label `key`) is already in spec.nodes with exactly this env (compared by name
    and value, so server-side defaults on the entry don't matter)."""
    want = sorted((e["name"], str(e.get("value"))) for e in env)
    hits = [n for n in nodes or [] if _selects(n, key)]
    return len(hits) == 1 and sorted((e["name"], str(e.get("value"))) for e in hits[0].get("env") or []) == want


def mig_stage(sc, key):
    """Where the live StorageCluster is: baseline (iSCSI, no override), enabled (iSCSI + our node override), final
    (NVMe/TCP for the cluster), finalizing (NVMe/TCP and our override still there) or unknown."""
    spec = (sc or {}).get("spec") or {}
    san = px_env_get(spec.get("env"), SAN_TYPE, "ISCSI")
    ours = any(_selects(n, key) for n in spec.get("nodes") or [])
    if san == "ISCSI":
        return "enabled" if ours else "baseline"
    if san == "NVMEOF-TCP":
        return "finalizing" if ours else "final"
    return "unknown"


def image_version_ge(image, minimum):
    """'portworx/px-pure-csi-driver:26.3.0' >= '26.3.0'. Suffixes (-ea1, -rc2) are ignored; no tag -> False."""
    if ":" not in str(image):
        return False
    tag = str(image).rsplit(":", 1)[-1].split("-", 1)[0]
    have = tuple(int(x) for x in re.findall(r"\d+", tag))
    want = tuple(int(x) for x in re.findall(r"\d+", minimum))
    return bool(have) and have >= want


# --- Kubernetes objects --------------------------------------------------------------------------------------------
def crd_has_path(crd, path):
    """True when a served version of the CRD's schema has `path`, e.g. 'spec.nodes[].env' ([] = list items)."""
    for version in ((crd or {}).get("spec") or {}).get("versions") or []:
        schema = ((version.get("schema") or {}).get("openAPIV3Schema")) or {}
        ok = True
        for token in path.split("."):
            is_list = token.endswith("[]")
            schema = (schema.get("properties") or {}).get(token[:-2] if is_list else token)
            if schema is None:
                ok = False
                break
            if is_list:
                schema = schema.get("items") or {}
        if ok:
            return True
    return False


def crd_ref(crds, kind):
    """CRD list -> {api_version, kind, scope, plural} of the CRD defining `kind`; {} if none."""
    for crd in crds or []:
        spec = crd.get("spec") or {}
        if (spec.get("names") or {}).get("kind") != kind:
            continue
        version = next((v for v in spec.get("versions") or [] if v.get("storage")), None) \
            or next(iter(spec.get("versions") or []), {})
        return {"api_version": f"{spec['group']}/{version.get('name')}", "kind": kind, "scope": spec.get("scope"),
                "plural": spec["names"].get("plural")}
    return {}


def node_plugin_container(pod, pattern=r"^(px-csi-node|node-plugin|px-node-plugin)$"):
    """The container of a PX-CSI node plugin pod that holds the driver's log. The docs name it px-csi-node,
    node-plugin and px-node-plugin (anchored: the pod also has sidecars such as csi-node-driver-registrar), so it is
    read from the pod; an error lists the names when there is not exactly one match."""
    names = [c["name"] for c in ((pod or {}).get("spec") or {}).get("containers") or []]
    hit = [n for n in names if re.search(pattern, n)]
    if len(hit) != 1:
        raise AnsibleFilterError(f"expected one node plugin container matching /{pattern}/ in {names}")
    return hit[0]


def nns_has_addresses(nns, storage):
    """NodeNetworkState has every storage entry's address on the NIC with its MAC (what the NNCP should have set)."""
    ifaces = (((nns or {}).get("status") or {}).get("currentState") or {}).get("interfaces") or []
    by_mac = {str(i.get("mac-address", "")).lower(): i for i in ifaces}
    for s in storage or []:
        iface = by_mac.get(str(s["mac"]).lower())
        if not iface:
            return False
        have = [a.get("ip") for a in ((iface.get("ipv4") or {}).get("address") or [])]
        if s["address"] not in have:
            return False
    return True


# --- MachineConfig rollout, node by node ---------------------------------------------------------------------------
MCO_CUR = "machineconfiguration.openshift.io/currentConfig"
MCO_STATE = "machineconfiguration.openshift.io/state"
PAUSED_BY = "ocp-px-csi-migration/paused"


def _annotations(obj):
    return ((obj or {}).get("metadata") or {}).get("annotations") or {}


def _ready(node):
    for cond in ((node or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def mco_current_configs(nodes):
    """Node resources -> {node name: the rendered MachineConfig it runs now}."""
    return {n["metadata"]["name"]: _annotations(n).get(MCO_CUR, "") for n in nodes or []}


def mco_fresh_nodes(nodes, before, verified, only=None):
    """Nodes the MCO has just finished and that are worth checking: state Done, Ready, running another rendered config
    than at the start of the roll (`before`, from mco_current_configs), and not in `verified` yet. `only` limits the
    answer to those node names (the storage nodes)."""
    out = []
    for n in nodes or []:
        name = n["metadata"]["name"]
        cur = _annotations(n).get(MCO_CUR, "")
        if only is not None and name not in only:
            continue
        if (_annotations(n).get(MCO_STATE) == "Done" and _ready(n) and cur != (before or {}).get(name, cur)
                and name not in (verified or [])):
            out.append(name)
    return out


def mco_degraded_nodes(nodes, only=None):
    """Nodes whose MCO state is Degraded or Unreconcilable (the daemon gave up on the update)."""
    return [n["metadata"]["name"] for n in nodes or []
            if _annotations(n).get(MCO_STATE) in ("Degraded", "Unreconcilable")
            and (only is None or n["metadata"]["name"] in only)]


def mco_pools_rolled(pools, roles, source="99-{role}-pure-nvme-prep"):
    """Every pool of `roles` has our MachineConfig among its sources, has reached its rendered config, is Updated and has
    all its machines updated."""
    by_name = {p["metadata"]["name"]: p for p in pools or []}
    for role in roles:
        pool = by_name.get(role)
        if not pool:
            return False
        want = ((pool.get("spec") or {}).get("configuration") or {})
        status = pool.get("status") or {}
        updated = any(c.get("type") == "Updated" and c.get("status") == "True" for c in status.get("conditions") or [])
        if (source.format(role=role) not in [s.get("name") for s in want.get("source") or []]
                or (status.get("configuration") or {}).get("name", "") != want.get("name", "-")
                or not updated or status.get("updatedMachineCount", 0) != status.get("machineCount", -1)):
            return False
    return True


def mco_paused_by_us(pool):
    """A pool this project paused after a failed per-node check (it carries our annotation)."""
    return bool((pool or {}).get("spec", {}).get("paused")) and PAUSED_BY in _annotations(pool)


def mco_paused_pools(pools):
    """The pools of the list that this project paused (a filter: Jinja's select() takes tests, not filters)."""
    return [p for p in pools or [] if mco_paused_by_us(p)]


# --- probe output --------------------------------------------------------------------------------------------------
def probe_traddrs(value):
    """The probe's nvme_addresses ('traddr=1.2.3.4,trsvcid=4420,...;...') -> the controllers' traddr values."""
    out = []
    for item in str(value or "").split(";"):
        m = re.search(r"(?:^|,)traddr=([^,]+)", item)
        if m:
            out.append(m.group(1))
    return out


def addrs_in_cidrs(addrs, cidrs):
    """Every address lies inside one of the CIDRs (and there is at least one)."""
    nets = [ipaddress.ip_network(x) for x in cidrs or []]
    addrs = list(addrs or [])
    return bool(addrs) and all(any(ipaddress.ip_address(a) in n for n in nets) for a in addrs)


# --- Ignition ------------------------------------------------------------------------------------------------------
def ign_file(content, path, mode=420):
    """Ignition file entry (data URL, overwrite) for a MachineConfig."""
    data = base64.b64encode(str(content).encode()).decode()
    return {"path": path, "mode": int(mode), "overwrite": True,
            "contents": {"source": f"data:text/plain;charset=utf-8;base64,{data}"}}


class FilterModule:
    def filters(self):
        return {
            "mig_model": mig_model,
            "mig_before_snapshot": mig_before_snapshot,
            "fa_from_secret": fa_from_secret,
            "fa_pod_path": fa_pod_path,
            "nvme_portals": nvme_portals,
            "fa_host_with": fa_host_with,
            "fa_host_connections": fa_host_connections,
            "fa_host_volumes_for": fa_host_volumes_for,
            "px_env_set": px_env_set,
            "px_env_get": px_env_get,
            "px_env_drop_iscsi": px_env_drop_iscsi,
            "px_nodes_entry": px_nodes_entry,
            "px_nodes_with": px_nodes_with,
            "px_nodes_without": px_nodes_without,
            "px_node_override_ok": px_node_override_ok,
            "mig_stage": mig_stage,
            "image_version_ge": image_version_ge,
            "crd_has_path": crd_has_path,
            "crd_ref": crd_ref,
            "node_plugin_container": node_plugin_container,
            "nns_has_addresses": nns_has_addresses,
            "mco_current_configs": mco_current_configs,
            "mco_fresh_nodes": mco_fresh_nodes,
            "mco_degraded_nodes": mco_degraded_nodes,
            "mco_pools_rolled": mco_pools_rolled,
            "mco_paused_by_us": mco_paused_by_us,
            "mco_paused_pools": mco_paused_pools,
            "probe_traddrs": probe_traddrs,
            "addrs_in_cidrs": addrs_in_cidrs,
            "ign_file": ign_file,
        }
