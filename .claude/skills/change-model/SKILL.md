---
name: change-model
description: Add, rename, require or validate a setting of the migration config (clusters/<name>.yaml, validated by mig_model). Gives the decision order for where a value should live and the full touch list across cluster.example.yaml, plugins/filter/nvme_migration.py, tests/model_test.py, the README and the role defaults. Use whenever a new input, a new required field, a new tunable or a new validation is wanted, or when someone asks the user to type something the cluster could tell us.
---

# Changing the config model

The config is the tool's whole input and, for the starting state, the only record a rollback may have. Every field is
something a customer must understand and can get wrong, so a field has to earn its place.

## Where should the value live? (first match wins)
1. **The live cluster or the array can tell us**: do not ask for it. Precedents: the realm is read from the `px-pure-secret`,
   the pod from the StorageClass, a NIC is found by its MAC in the node's NodeNetworkState, the portals come from the array.
   If a config value could only disagree with what the cluster says, refuse it (`flasharray.pod` and `.realm` raise an error).
2. **Only the site knows** (switch ports, VLAN ids, who to call): free text in `migration.operator_note` /
   `operator_note_rollback`. Do not add platform words (hypervisor, port group, VM) to a role, filter or the model: the
   scenario is generic ("the network behind each storage NIC changes while the node is drained").
3. **A secret**: never in the YAML. The config holds the name of an environment variable and a file path
   (`token_env`, `token_file`).
4. **A starting state a rollback needs**: a required field, a blank one refused, and a preflight check against the live nodes
   (`networks.current`, each NIC's MAC and current address, `operator_note_rollback`).
5. **A tunable with a sane default**: a role default, overridable with `-e` (precedent: `mig_drain_wait` in
   `roles/nvme_migrate/defaults/main.yml`), not a config field.

## Touch list
- `plugins/filter/nvme_migration.py`, `mig_model`: validate (the error names the field path), default, and return it in the
  model. A new helper is registered in the filter table at the bottom of the file.
- `tests/model_test.py`: start from a deep copy of the example config, as the existing cases do. One accepting case, one
  refusal per rule (missing, blank, wrong type, forbidden), unit tests for any helper. Only invented values.
- `cluster.example.yaml`: the field with its comment, marked REQUIRED where it is. Documentation addresses only
  (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, and 198.18.0.0/15 for the iSCSI side), `example.com`, locally
  administered MACs.
- `README.md`, "What you give it" (and the phase table or troubleshooting if behavior changed); CLAUDE.md if it adds a rule.
- Your own `clusters/<name>.yaml` (gitignored) so the live `--check` still loads. Never copy a value from it into a tracked file.

A field that a running migration depends on is a compatibility decision: an existing config that stops loading blocks a
rollback in the middle of a migration. Prefer a default, or a clear error that says what to add.

Then follow `verify-change`, and `publish` before a push.
