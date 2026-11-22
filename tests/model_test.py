"""Unit tests for the migration model and the filter plugins. Nothing here needs a cluster, an array or any other project.

    python3 tests/model_test.py        (needs ansible-core and PyYAML: the toolbox image, or any Ansible machine)
Route A (Docker): docker compose run --rm -T toolbox python3 tests/model_test.py"""
import base64
import copy
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins", "filter"))

from ansible.errors import AnsibleFilterError  # noqa: E402
from nvme_k8s import (k8s_condition, k8s_csinodes_with_driver, k8s_node_readiness, k8s_unhealthy_operators,  # noqa: E402
                      kv_lines, nmstate_storage_interfaces, nns_ifaces_by_mac)
from nvme_migration import (addrs_in_cidrs, crd_has_path, crd_ref, fa_from_secret, fa_host_connections,  # noqa: E402
                            fa_host_volumes_for, fa_host_with, fa_pod_path, ign_file, image_version_ge, mco_current_configs, mco_degraded_nodes, mco_fresh_nodes,
                            mco_paused_by_us, mco_paused_pools, mco_pools_rolled, mig_before_snapshot, mig_model, mig_stage,
                            nns_has_addresses, node_plugin_container, nvme_portals, probe_traddrs, px_env_drop_iscsi,
                            px_env_get, px_env_set, px_node_override_ok, px_nodes_entry, px_nodes_with,
                            px_nodes_without)

EXAMPLE = yaml.safe_load(open(os.path.join(HERE, "..", "cluster.example.yaml")))
failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def rejects(name, mutate, fragment):
    raw = copy.deepcopy(EXAMPLE)
    mutate(raw)
    try:
        mig_model(raw)
    except AnsibleFilterError as exc:
        check(f"{name} (rejected: {fragment})", fragment in str(exc))
        return
    check(f"{name} (should have been rejected)", False)


# --- the model, from the example config ----------------------------------------------------------------------------------
m = mig_model(copy.deepcopy(EXAMPLE))
n0 = m["nodes"][0]
check("model: independent of any other project (nothing like a cluster model was imported)",
      not any(name in sys.modules for name in ("cluster_model", "k8s")))
check("model: cluster and PX settings", (m["cluster"]["name"], m["portworx"]["namespace"], m["portworx"]["storageclass"])
      == ("lab", "portworx", "px-fa"))
check("model: the array is the config's endpoint, with the token's sources defaulting to the env var and a file under .state",
      m["flasharray"] == {"endpoint": "192.0.2.40", "token_env": "FA_API_TOKEN", "token_file": ".state/fa-token"})
check("model: label, multipath, demo namespace, pools", (m["label"], m["multipath"], m["demo_namespace"], m["mc_pools"])
      == ({"key": "storage-protocol", "value": "nvme-tcp"}, "auto", "px-nvme-demo", ["master"]))
check("model: target CIDRs", m["cidrs"] == ["198.51.100.0/24", "203.0.113.0/24"])
check("model: nodes in order, hostname as given or defaulting to the name",
      [(n["name"], n["hostname"]) for n in m["nodes"]] == [("node0", "node0.example.com"), ("node1", "node1.example.com")])
check("model: per NIC the network and address now and after",
      [(nic["old"]["name"], nic["old"]["cidr"], nic["old"]["address"], nic["new"]["name"], nic["new"]["cidr"], nic["new"]["address"])
       for nic in n0["nics"]]
      == [("iscsi-a", "198.18.30.0/24", "198.18.30.101", "nvme-a", "198.51.100.0/24", "198.51.100.101"),
          ("iscsi-b", "198.18.31.0/24", "198.18.31.101", "nvme-b", "203.0.113.0/24", "203.0.113.101")])
check("model: NMState inputs carry name, mac, address, prefix, mtu (default 1500)",
      n0["current_storage"][1] == {"name": "iscsi-b", "mac": "02:00:00:00:31:01", "address": "198.18.31.101", "prefix": 24, "mtu": 1500}
      and n0["target_storage"][0]["prefix"] == 24)
check("model: the same MACs before and after", [s["mac"] for s in n0["current_storage"]] == [s["mac"] for s in n0["target_storage"]])
check("model: nothing platform-specific in it", not any(w in json.dumps(m).lower() for w in ("vsphere", "portgroup", "port group", "vcenter")))
minimal = copy.deepcopy(EXAMPLE)
minimal.pop("portworx")
minimal["migration"] = {"operator_note_rollback": "Put the switch ports back to VLAN 30 and 31."}   # the one required key
minimal["nodes"][0].pop("hostname")
mm = mig_model(minimal)
check("model: defaults (px namespace/class, label, no pod needed)",
      (mm["portworx"], mm["label"]["key"], mm["nodes"][0]["hostname"]) == ({"namespace": "portworx", "storageclass": "px-fa"}, "storage-protocol", "node0"))
check("model: the NMState policy name defaults to storage-{node}", m["nmstate_policy"] == "storage-{node}")
rejects("model: the policy name needs {node}", lambda r: r.setdefault("nmstate", {}).update(policy="storage"), "must contain {node}")
macs_upper = copy.deepcopy(EXAMPLE)
macs_upper["nodes"][0]["nics"][0]["mac"] = "02:00:00:00:30:0A"
check("model: MACs are lowercased", mig_model(macs_upper)["nodes"][0]["current_storage"][0]["mac"] == "02:00:00:00:30:0a")

rejects("model: no cluster name", lambda r: r["cluster"].pop("name"), "cluster.name is required")
rejects("model: no array endpoint", lambda r: r["flasharray"].pop("endpoint"), "flasharray.endpoint is required")
rejects("model: no flasharray section", lambda r: r.pop("flasharray"), "flasharray.endpoint is required")
rejects("model: the pod is not configured (it comes from the StorageClass)", lambda r: r["flasharray"].update(pod="realm1::px-csi"), "remove them")
rejects("model: the realm is not configured (it comes from the secret)", lambda r: r["flasharray"].update(realm="realm1"), "remove them")
rejects("model: no MachineConfigPools", lambda r: r.pop("machineconfig_pools"), "machineconfig_pools is required")
rejects("model: label without =", lambda r: r["migration"].update(label="nvme"), "must be key=value")
rejects("model: multipath value", lambda r: r["migration"].update(multipath="maybe"), "auto, Y or N")
rejects("model: the rollback note is required (nothing else records the physical network)",
        lambda r: r["migration"].pop("operator_note_rollback"), "operator_note_rollback is required")
rejects("model: a blank rollback note is no note", lambda r: r["migration"].update(operator_note_rollback="  \n"), "operator_note_rollback is required")
rejects("model: no migration section at all", lambda r: r.pop("migration"), "operator_note_rollback is required")
rejects("model: networks must pair up", lambda r: r["networks"]["target"].pop(), "same number")
rejects("model: a network needs a CIDR", lambda r: r["networks"]["current"][0].pop("cidr"), "missing 'cidr'")
rejects("model: a CIDR with host bits set", lambda r: r["networks"]["target"][0].update(cidr="198.51.100.5/24"), "has host bits set")
rejects("model: a network defined twice", lambda r: r["networks"]["target"][1].update(name="nvme-a"), "defined twice")
rejects("model: mtu is a number", lambda r: r["networks"]["current"][0].update(mtu="big"), "mtu must be a number")
rejects("model: no nodes", lambda r: r.pop("nodes"), "nodes is required")
rejects("model: one NIC entry per network", lambda r: r["nodes"][0]["nics"].pop(), "one entry per network")
rejects("model: a bad MAC", lambda r: r["nodes"][0]["nics"][0].update(mac="02:00:00:00:30"), "is not aa:bb:cc:dd:ee:ff")
rejects("model: a MAC used twice", lambda r: r["nodes"][1]["nics"][0].update(mac="02:00:00:00:30:01"), "used twice")
rejects("model: an address outside its network", lambda r: r["nodes"][0]["nics"][0].update(address="198.18.31.101"), "not a usable host")
rejects("model: the network address itself", lambda r: r["nodes"][0]["nics"][0].update(target_address="198.51.100.0"), "not a usable host")
rejects("model: an address used twice", lambda r: r["nodes"][1]["nics"][0].update(address="198.18.30.101"), "used by another NIC")
rejects("model: a target address used twice", lambda r: r["nodes"][1]["nics"][1].update(target_address="203.0.113.101"), "used by another NIC")
rejects("model: not an IP address", lambda r: r["nodes"][0]["nics"][0].update(target_address="soon"), "is not an IP address")
rejects("model: a node defined twice", lambda r: r["nodes"][1].update(name="node0"), "defined twice")
rejects("model: a hostname used twice", lambda r: r["nodes"][1].update(hostname="node0.example.com"), "defined twice")

# --- the array PX-CSI uses: its secret and its StorageClass ----------------------------------------------------------------
TOKEN = "11111111-2222-3333-4444-555555555555"


def secret_of(doc):
    return {"data": {"pure.json": base64.b64encode(doc if isinstance(doc, bytes) else json.dumps(doc).encode()).decode()}}


one = {"FlashArrays": [{"MgmtEndPoint": "192.0.2.40", "APIToken": TOKEN, "Realm": "realm1"}]}
two = {"FlashArrays": [{"MgmtEndPoint": "192.0.2.41", "APIToken": TOKEN, "Realm": "realm9"}, one["FlashArrays"][0]]}
got = fa_from_secret(secret_of(one), "192.0.2.40")
check("secret: the config's array is in pure.json: its realm comes back, and never the token",
      (got["error"], got["realm"], got["endpoints"]) == ("", "realm1", ["192.0.2.40"]) and TOKEN not in json.dumps(got))
check("secret: with several arrays the config's one picks the realm", fa_from_secret(secret_of(two), "192.0.2.40")["realm"] == "realm1")
check("secret: the address is compared as written, case apart (an FQDN is not an IP)",
      fa_from_secret(secret_of({"FlashArrays": [{"MgmtEndPoint": "FA.example.com", "APIToken": TOKEN}]}), "fa.example.com")["error"] == ""
      and fa_from_secret(secret_of(one), "fa.example.com")["error"] != "")
check("secret: no realm is an empty realm (the realm is for secure multi-tenancy only)",
      fa_from_secret(secret_of({"FlashArrays": [{"MgmtEndPoint": "192.0.2.40", "APIToken": TOKEN}]}), "192.0.2.40")["realm"] == "")
bad = {
    "no secret": (None, "192.0.2.40"),
    "no pure.json key": ({"data": {"other": "eA=="}}, "192.0.2.40"),
    "invalid JSON": (secret_of(b"{not json " + TOKEN.encode()), "192.0.2.40"),
    "no FlashArrays list": (secret_of({"FlashBlades": []}), "192.0.2.40"),
    "the config's array is not listed": (secret_of(one), "192.0.2.99"),
    "no endpoint in the config": (secret_of(one), ""),
}
for why, (sec, ep) in bad.items():
    r = fa_from_secret(sec, ep)
    check(f"secret: {why} is an error that never contains the token", r["error"] != "" and TOKEN not in json.dumps(r))
check("secret: an unlisted array is refused by naming the listed ones",
      "192.0.2.40" in fa_from_secret(secret_of(one), "192.0.2.99")["error"] and "192.0.2.99" in fa_from_secret(secret_of(one), "192.0.2.99")["error"])
sc = {"parameters": {"backend": "pure_block", "pure_fa_pod_name": "px-csi"}}
check("pod: the StorageClass's pod, qualified with the secret's realm", fa_pod_path(sc, "realm1") == "realm1::px-csi")
check("pod: already qualified, or no realm: as written",
      (fa_pod_path({"parameters": {"pure_fa_pod_name": "realm2::p"}}, "realm1"), fa_pod_path(sc, "")) == ("realm2::p", "px-csi"))
check("pod: none when the StorageClass names none", (fa_pod_path({"parameters": {"backend": "pure_block"}}, "realm1"), fa_pod_path(None, "r")) == ("", ""))

# --- the before-snapshot of a node's storage network (what a rollback restores) --------------------------------------------
def _iface(name, mac, ip=None):
    i = {"name": name, "type": "ethernet", "state": "up", "mtu": 1500, "mac-address": mac, "lldp": {"enabled": False},
         "ipv6": {"enabled": False}, "ipv4": {"enabled": bool(ip), "dhcp": False}}
    if ip:
        i["ipv4"]["address"] = [{"ip": ip, "prefix-length": 24}]
    return i


nns0 = {"status": {"currentState": {"interfaces": [_iface("ens3", "02:00:00:00:00:01", "192.0.2.10"),
                                                    _iface("ens34", "02:00:00:00:30:01".upper(), "198.18.30.101"),
                                                    _iface("ens35", "02:00:00:00:31:01".upper(), "198.18.31.101")]}}}
nncp0 = {"metadata": {"name": "storage-node0", "labels": {"app.kubernetes.io/managed-by": "someone"}, "generation": 3, "uid": "x"},
         "spec": {"nodeSelector": {"kubernetes.io/hostname": "node0.example.com"}, "desiredState": {"interfaces": [{"name": "ens34"}]}}}
snap = mig_before_snapshot(n0, nns0, nncp0, "2026-01-01T00:00:00Z")
check("snapshot: node, hostname, time", (snap["node"], snap["hostname"], snap["taken"]) == ("node0", "node0.example.com", "2026-01-01T00:00:00Z"))
check("snapshot: one entry per storage NIC, found by MAC whatever its case, with the network it leaves",
      [(s["mac"], s["network"], s["interface"]["name"]) for s in snap["storage_nics"]]
      == [("02:00:00:00:30:01", "iscsi-a", "ens34"), ("02:00:00:00:31:01", "iscsi-b", "ens35")])
check("snapshot: the interface keeps its addresses, MTU and state, and not the noise (lldp)",
      snap["storage_nics"][0]["interface"]["ipv4"]["address"] == [{"ip": "198.18.30.101", "prefix-length": 24}]
      and snap["storage_nics"][0]["interface"]["mtu"] == 1500 and "lldp" not in snap["storage_nics"][0]["interface"])
check("snapshot: the policy as it is (name, labels, spec), without the server's bookkeeping",
      snap["policy"] == {"name": "storage-node0", "labels": {"app.kubernetes.io/managed-by": "someone"}, "spec": nncp0["spec"]})
check("snapshot: a node without a policy records none", mig_before_snapshot(n0, nns0, None)["policy"] is None)
check("snapshot: it survives a YAML round trip (the file it is kept in)", yaml.safe_load(yaml.safe_dump(snap)) == snap)
try:
    mig_before_snapshot(n0, {"status": {"currentState": {"interfaces": [_iface("ens3", "02:00:00:00:00:01")]}}}, None)
    check("snapshot: a NIC missing from the NodeNetworkState is refused", False)
except AnsibleFilterError as exc:
    check("snapshot: a NIC missing from the NodeNetworkState is refused (nothing to record)", "nothing to record" in str(exc))

# --- Kubernetes object readers ------------------------------------------------------------------------------------------
res = {"status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "Degraded", "status": "False"}]}}
check("k8s_condition: the status of a condition, or empty", (k8s_condition(res, "Ready"), k8s_condition(res, "Nope"), k8s_condition({}, "Ready")) == ("True", "", ""))
check("k8s_node_readiness", k8s_node_readiness([dict(res, metadata={"name": "a"}), {"metadata": {"name": "b"}}])
      == [{"name": "a", "ready": True}, {"name": "b", "ready": False}])
ops = [{"metadata": {"name": "ok"}, "status": {"conditions": [{"type": "Available", "status": "True"}, {"type": "Degraded", "status": "False"}]}},
       {"metadata": {"name": "down"}, "status": {"conditions": [{"type": "Available", "status": "False"}]}},
       {"metadata": {"name": "degraded"}, "status": {"conditions": [{"type": "Available", "status": "True"}, {"type": "Degraded", "status": "True"}]}}]
check("k8s_unhealthy_operators: not Available or Degraded", k8s_unhealthy_operators(ops) == ["down", "degraded"])
csi = [{"metadata": {"name": "a"}, "spec": {"drivers": [{"name": "pxd.portworx.com"}]}}, {"metadata": {"name": "b"}, "spec": {"drivers": []}}]
check("k8s_csinodes_with_driver", k8s_csinodes_with_driver(csi, "pxd.portworx.com") == ["a"])
check("kv_lines: key=value lines, others ignored", kv_lines(["a=1", "noise", "b=x=y"]) == {"a": "1", "b": "x=y"})
nns = {"status": {"currentState": {"interfaces": [
    {"name": "ens34", "mac-address": "00:50:56:01:00:A1", "ipv4": {"address": [{"ip": "198.51.100.101", "prefix-length": 24}]}},
    {"name": "ens35", "mac-address": "00:50:56:01:00:A2", "ipv4": {"address": []}}, {"name": "lo"}]}}}
check("nns_ifaces_by_mac: lowercase MAC -> name, only NICs with a MAC", nns_ifaces_by_mac(nns) == {"00:50:56:01:00:a1": "ens34", "00:50:56:01:00:a2": "ens35"})
nm = nmstate_storage_interfaces([n0["target_storage"][0]], {"02:00:00:00:30:01": "ens34"})
check("nmstate_storage_interfaces: static IPv4 only, found by MAC",
      nm == [{"name": "ens34", "type": "ethernet", "state": "up", "mtu": 1500,
              "ipv4": {"enabled": True, "dhcp": False, "address": [{"ip": "198.51.100.101", "prefix-length": 24}]},
              "ipv6": {"enabled": False}}])
try:
    nmstate_storage_interfaces([n0["target_storage"][0]], {})
    check("nmstate_storage_interfaces: a MAC the node does not have is refused", False)
except AnsibleFilterError as exc:
    check("nmstate_storage_interfaces: a MAC the node does not have is refused", "no interface with MAC" in str(exc))
check("nns_has_addresses: by MAC, case-insensitive; false when an address is missing",
      nns_has_addresses(nns, [{"mac": "00:50:56:01:00:a1", "address": "198.51.100.101"}])
      and not nns_has_addresses(nns, [{"mac": "00:50:56:01:00:a1", "address": "198.51.100.101"}, {"mac": "00:50:56:01:00:a2", "address": "203.0.113.101"}])
      and not nns_has_addresses(nns, [{"mac": "00:50:56:01:00:ff", "address": "198.51.100.101"}]))

# --- FlashArray (purefa_info shapes) -------------------------------------------------------------------------------------
net = {
    "ct0.eth4": {"address": "198.51.100.40", "services": ["nvme-tcp", "iscsi"]},
    "ct1.eth4": {"address": "198.51.100.41", "services": ["nvme-tcp"]},
    "ct0.eth5": {"address": "203.0.113.40", "services": ["nvme-tcp"]},
    "ct0.eth6": {"address": "10.9.9.9", "services": ["nvme-tcp"]},
    "ct0.eth7": {"address": "198.18.30.40", "services": ["iscsi"]},
    "ct0.eth8": {"address": "203.0.113.41", "services": ["nvme-tcp"], "enabled": False},
    "ct0.eth9": {"address": "0.0.0.0", "services": ["nvme-tcp"]},
    "vir0": {"address": "192.0.2.1", "services": ["management"]},
}
portals = nvme_portals(net, ["198.51.100.0/24", "203.0.113.0/24"])
check("nvme_portals: NVMe/TCP, enabled, addressed interfaces only", [p["interface"] for p in portals] == ["ct0.eth4", "ct0.eth5", "ct0.eth6", "ct1.eth4"])
check("nvme_portals: each carries its target CIDR, or none when outside",
      {p["interface"]: p["cidr"] for p in portals} == {"ct0.eth4": "198.51.100.0/24", "ct1.eth4": "198.51.100.0/24", "ct0.eth5": "203.0.113.0/24", "ct0.eth6": ""})
check("nvme_portals: no interfaces, no portals", nvme_portals({}, ["198.51.100.0/24"]) == [])
hosts = {"realm::master0-1": {"iqn": ["iqn.1994-05.com.redhat:a"], "nqn": [], "volumes": ["v1", "v2"]},
         "realm::master1-2": {"iqn": [], "nqn": ["nqn.2014-08.org.nvmexpress:uuid:AAA"]},
         "other": {"iqn": "iqn.x", "nqn": ""}}
check("fa_host_with: by IQN", fa_host_with(hosts, "iqn", "iqn.1994-05.com.redhat:a")["name"] == "realm::master0-1")
check("fa_host_with: by NQN, case-insensitive", fa_host_with(hosts, "nqn", "nqn.2014-08.org.nvmexpress:uuid:aaa")["name"] == "realm::master1-2")
check("fa_host_with: a string value counts as a one-item list", fa_host_with(hosts, "iqn", "iqn.x")["name"] == "other")
check("fa_host_with: none, or an empty value, is {}", fa_host_with(hosts, "nqn", "nqn.none") == {} and fa_host_with(hosts, "nqn", "") == {})
check("fa_host_connections: counted, or -1 when purefa_info doesn't say",
      (fa_host_connections(hosts["realm::master0-1"]), fa_host_connections(hosts["realm::master1-2"]), fa_host_connections({})) == (2, -1, -1))
px_host = {"name": "realm::master0-1", "volumes": [
    {"volume": "realm::px-csi::px_aaba3807-pvc-1111", "lun": None, "nsid": 1},
    {"volume": "realm::px-csi::px_aaba3807-pvc-2222", "lun": None, "nsid": 2},
    {"volume": "realm::px-csi::px_aaba3807-pvc-33334", "lun": None, "nsid": 3}]}
check("fa_host_volumes_for: the PVs that have a connected array volume, in the order asked",
      fa_host_volumes_for(px_host, ["pvc-2222", "pvc-1111"]) == ["pvc-2222", "pvc-1111"])
check("fa_host_volumes_for: a PV without a connection is left out, a longer id does not match a shorter one",
      fa_host_volumes_for(px_host, ["pvc-1111", "pvc-4444", "pvc-3334"]) == ["pvc-1111"])
check("fa_host_volumes_for: plain names, no volumes, no host, no PVs",
      fa_host_volumes_for({"volumes": ["x::px_a-pvc-9"]}, ["pvc-9"]) == ["pvc-9"]
      and fa_host_volumes_for({}, ["pvc-9"]) == [] and fa_host_volumes_for(px_host, []) == [] and fa_host_volumes_for(px_host, [""]) == [])

# --- StorageCluster -----------------------------------------------------------------------------------------------------
env = [{"name": "PURE_FLASHARRAY_SAN_TYPE", "value": "ISCSI"}, {"name": "ENABLE_SUBNET_AWARE_ISCSI_LOGIN", "value": "true"},
       {"name": "PURE_ISCSI_ALLOWED_IFACES", "value": "ens34,ens35"}, {"name": "PURE_ISCSI_ALLOWED_CIDRS", "value": "198.18.30.0/24,198.18.31.0/24"},
       {"name": "PX_LOGLEVEL", "value": "debug"}]
final = px_env_set(px_env_drop_iscsi(env), "PURE_FLASHARRAY_SAN_TYPE", "NVMEOF-TCP")
check("env: iSCSI settings dropped, nothing else (PX_LOGLEVEL stays), SAN type replaced in place, no CIDR filter added",
      [(e["name"], e["value"]) for e in final] == [("PURE_FLASHARRAY_SAN_TYPE", "NVMEOF-TCP"), ("PX_LOGLEVEL", "debug")])
check("env: setting twice changes nothing", px_env_set(final, "PURE_FLASHARRAY_SAN_TYPE", "NVMEOF-TCP") == final)
with_cidrs = px_env_set(final, "PURE_NVME_ALLOWED_CIDRS", "198.51.100.0/24,203.0.113.0/24")
check("env: the CIDR filter (only when the array has portals outside the target networks) is appended, once",
      [(e["name"], e["value"]) for e in with_cidrs][-1] == ("PURE_NVME_ALLOWED_CIDRS", "198.51.100.0/24,203.0.113.0/24")
      and px_env_set(with_cidrs, "PURE_NVME_ALLOWED_CIDRS", "198.51.100.0/24,203.0.113.0/24") == with_cidrs)
check("env: get with a default", (px_env_get(env, "PX_LOGLEVEL"), px_env_get(env, "NOPE", "d")) == ("debug", "d"))
KEY = "storage-protocol"
node_env = [{"name": "PURE_FLASHARRAY_SAN_TYPE", "value": "NVMEOF-TCP"}]
entry = px_nodes_entry(KEY, "nvme-tcp", node_env)
other = {"selector": {"nodeName": "x"}, "env": [{"name": "MAX_VOLUMES_PER_NODE", "value": "10"}]}
check("nodes entry: labelSelector with the label, and the env", entry == {"selector": {"labelSelector": {"matchLabels": {KEY: "nvme-tcp"}}}, "env": node_env})
added = px_nodes_with([other], entry, KEY)
check("nodes: our entry is added, others stay", added == [other, entry])
check("nodes: adding twice is the same list", px_nodes_with(added, entry, KEY) == added)
check("nodes: removing ours keeps the others, and the last removal leaves nothing", (px_nodes_without(added, KEY), px_nodes_without([entry], KEY)) == ([other], []))
check("nodes: override present with the same env (extra server defaults on the entry don't matter)",
      px_node_override_ok([dict(entry, extra="default")], KEY, node_env) is True and px_node_override_ok([other], KEY, node_env) is False
      and px_node_override_ok([entry], KEY, node_env + [{"name": "PURE_NVME_ALLOWED_CIDRS", "value": "x"}]) is False)


def sc(env_list, nodes_list=None):
    return {"spec": {"env": env_list, **({"nodes": nodes_list} if nodes_list else {})}}


check("stage: baseline, enabled, finalizing, final, unknown",
      [mig_stage(sc(env), KEY), mig_stage(sc(env, [entry]), KEY), mig_stage(sc(final, [entry]), KEY), mig_stage(sc(final), KEY),
       mig_stage(sc([{"name": "PURE_FLASHARRAY_SAN_TYPE", "value": "FC"}]), KEY)] == ["baseline", "enabled", "finalizing", "final", "unknown"])
check("stage: no SAN type in the env means the CRD default, iSCSI", mig_stage({"spec": {}}, KEY) == "baseline")
check("image version: >= 26.3.0 (suffixes ignored, untagged is not enough)",
      [image_version_ge(i, "26.3.0") for i in ("portworx/px-pure-csi-driver:26.3.0", "x:26.3.1", "x:26.4.0-ea1", "x:26.2.2", "x:25.8.1", "x")] == [True, True, True, False, False, False])

# --- Kubernetes objects -------------------------------------------------------------------------------------------------
crd = {"spec": {"versions": [{"name": "v1", "storage": True, "schema": {"openAPIV3Schema": {"properties": {"spec": {"properties": {
    "nodes": {"type": "array", "items": {"properties": {"env": {"type": "array"}, "selector": {"properties": {"labelSelector": {}, "nodeName": {}}}}}}}}}}}}]}}
check("crd_has_path: list items and nested objects", crd_has_path(crd, "spec.nodes[].env") and crd_has_path(crd, "spec.nodes[].selector.labelSelector"))
check("crd_has_path: missing path", not crd_has_path(crd, "spec.nodes[].nope") and not crd_has_path({}, "spec.nodes[].env"))
sni_crd = {"spec": {"group": "core.example.org", "scope": "Cluster", "names": {"kind": "StorageNodeInitiator", "plural": "storagenodeinitiators"},
                    "versions": [{"name": "v1alpha1", "storage": False}, {"name": "v1", "storage": True}]}}
check("crd_ref: group/version of the storage version, scope, plural; {} when absent",
      crd_ref([sni_crd], "StorageNodeInitiator") == {"api_version": "core.example.org/v1", "kind": "StorageNodeInitiator", "scope": "Cluster", "plural": "storagenodeinitiators"}
      and crd_ref([sni_crd], "Nope") == {})
pod = {"spec": {"containers": [{"name": "csi-node-driver-registrar"}, {"name": "node-plugin"}]}}
check("node_plugin_container: the one matching name (not the registrar sidecar)", node_plugin_container(pod) == "node-plugin")
try:
    node_plugin_container({"spec": {"containers": [{"name": "px-csi-node"}, {"name": "node-plugin"}]}})
    check("node_plugin_container: an ambiguous pod lists the names", False)
except AnsibleFilterError as exc:
    check("node_plugin_container: an ambiguous pod lists the names", "expected one node plugin container" in str(exc))

# --- MachineConfig roll: which nodes to check, when the pools are done -----------------------------------------------------
CUR, STATE = "machineconfiguration.openshift.io/currentConfig", "machineconfiguration.openshift.io/state"


def node(name, cfg, state="Done", ready="True"):
    return {"metadata": {"name": name, "annotations": {CUR: cfg, STATE: state}}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


before = mco_current_configs([node("m0", "old"), node("m1", "old"), node("m2", "old")])
check("mco_current_configs: node -> rendered config", before == {"m0": "old", "m1": "old", "m2": "old"})
mid = [node("m0", "new"), node("m1", "old", "Working"), node("m2", "old")]
check("mco_fresh_nodes: a node Done on another config than at the start", mco_fresh_nodes(mid, before, []) == ["m0"])
check("mco_fresh_nodes: not twice", mco_fresh_nodes(mid, before, ["m0"]) == [])
check("mco_fresh_nodes: not while Working or not Ready", mco_fresh_nodes([node("m0", "new", "Working"), node("m1", "new", ready="False")], before, []) == [])
check("mco_fresh_nodes: nothing fresh when the roll had nothing to do", mco_fresh_nodes([node("m0", "old"), node("m1", "old")], before, []) == [])
check("mco_fresh_nodes: a node unknown at the start is not fresh", mco_fresh_nodes([node("m9", "new")], before, []) == [])
check("mco_fresh_nodes: only the named (storage) nodes count",
      mco_fresh_nodes([node("m0", "new"), node("w0", "new")], dict(before, w0="old"), [], ["m0"]) == ["m0"]
      and mco_fresh_nodes([node("w0", "new")], dict(before, w0="old"), [], ["m0"]) == [])
check("mco_degraded_nodes: Degraded and Unreconcilable, optionally only the named nodes",
      mco_degraded_nodes([node("m0", "x", "Degraded"), node("m1", "x", "Unreconcilable"), node("m2", "x"), node("w0", "x", "Degraded")], ["m0", "m1", "m2"]) == ["m0", "m1"]
      and mco_degraded_nodes([node("m0", "x", "Working")]) == [])


def pool(name="master", src=("99-master-pure-iscsi", "99-master-pure-nvme-prep"), cfg="new", status_cfg="new", updated="True", done=3, total=3):
    return {"metadata": {"name": name}, "spec": {"configuration": {"name": cfg, "source": [{"name": s} for s in src]}},
            "status": {"configuration": {"name": status_cfg}, "conditions": [{"type": "Updated", "status": updated}], "updatedMachineCount": done, "machineCount": total}}


check("mco_pools_rolled: our MachineConfig in, rendered config reached, Updated, all machines updated", mco_pools_rolled([pool()], ["master"]))
check("mco_pools_rolled: not while a machine is still to update", not mco_pools_rolled([pool(done=1)], ["master"]))
check("mco_pools_rolled: not while the status config lags the spec", not mco_pools_rolled([pool(status_cfg="old")], ["master"]))
check("mco_pools_rolled: not while Updated is False", not mco_pools_rolled([pool(updated="False")], ["master"]))
check("mco_pools_rolled: not before the controller rendered our MachineConfig", not mco_pools_rolled([pool(src=("99-master-pure-iscsi",))], ["master"]))
check("mco_pools_rolled: every role, and a missing pool is not rolled",
      mco_pools_rolled([pool(), pool("worker", src=("99-worker-pure-nvme-prep",), total=0, done=0)], ["master", "worker"]) and not mco_pools_rolled([pool()], ["master", "worker"]))
paused = {"metadata": {"annotations": {"ocp-px-csi-migration/paused": "x"}}, "spec": {"paused": True}}
check("mco_paused_by_us: paused with our annotation only",
      mco_paused_by_us(paused) and not mco_paused_by_us({"metadata": {}, "spec": {"paused": True}})
      and not mco_paused_by_us({"metadata": {"annotations": {"ocp-px-csi-migration/paused": "x"}}, "spec": {"paused": False}}))
check("mco_paused_pools: the subset we paused, usable as a filter in Jinja", mco_paused_pools([paused, {"metadata": {}, "spec": {"paused": True}}, pool()]) == [paused] and mco_paused_pools([]) == [])

# --- probe output and Ignition -------------------------------------------------------------------------------------------
addrs = "traddr=198.51.100.40,trsvcid=4420,src_addr=198.51.100.101;traddr=203.0.113.40,trsvcid=4420;"
check("probe_traddrs: the controllers' target addresses", probe_traddrs(addrs) == ["198.51.100.40", "203.0.113.40"])
check("addrs_in_cidrs: all inside, none outside, at least one",
      addrs_in_cidrs(probe_traddrs(addrs), ["198.51.100.0/24", "203.0.113.0/24"])
      and not addrs_in_cidrs(probe_traddrs(addrs), ["198.51.100.0/24"]) and not addrs_in_cidrs([], ["198.51.100.0/24"]))
f = ign_file("nvme_tcp\n", "/etc/modules-load.d/nvme_tcp.conf")
check("ign_file: path, mode 0644 (420), overwrite, base64 data URL",
      f == {"path": "/etc/modules-load.d/nvme_tcp.conf", "mode": 420, "overwrite": True, "contents": {"source": "data:text/plain;charset=utf-8;base64,bnZtZV90Y3AK"}})

print(f"\n{len(failures)} failure(s)" if failures else "\nall ok")
sys.exit(1 if failures else 0)
