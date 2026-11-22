# clusters/

`<name>.yaml` is the migration config of one cluster (start from `../cluster.example.yaml`). It is gitignored: it holds real
addresses, MACs and cluster names. Every playbook takes `-e cluster=<name>`; the `cluster.name` inside must match.
The kubeconfig and the array token are not here: see the README ("What you give it").
