# End-to-end production cell

This opt-in root deploys the managed data/control services, hardened API and worker,
and a private Ollama-compatible Cloud Run GPU model service in one region. It wires
the model service URI directly into the worker as both `AGENT_MODEL_BASE_URL` and the
Google ID-token audience, and grants `roles/run.invoker` only to the worker identity.

The root is intentionally separate from the cost-free `dev` assessment root. A plan
requires reviewed immutable image digests, GPU capacity values, identities, secrets,
budget recipients, and a remote GCS backend. Never place secret values in tfvars; the
inputs here are Secret Manager resource IDs only.

```bash
cp terraform.tfvars.example /absolute/private/path/production-cell.tfvars

terraform init -reconfigure \
  -backend-config="bucket=PRODUCTION_TERRAFORM_STATE_BUCKET" \
  -backend-config="prefix=production/cell"

terraform plan -input=false \
  -var-file=/absolute/private/path/production-cell.tfvars
```

Run these commands from this directory. Apply only the reviewed binary plan after
GPU quota, cost, model-license, data-classification, residency, load, recovery, and
SLO approval. The assessment workflow never targets this root.

Run formatting and tests from a clean checkout (or a temporary `git archive`) before
planning, because local ignored Finder duplicate files can otherwise be loaded by
Terraform.
