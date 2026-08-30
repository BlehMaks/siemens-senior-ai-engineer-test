# Production model-plane reference

This root is separate from the assessment deployment. Its default
`model_plane_profile = "assessment"` creates no resources. The
`cloud_run_gpu` profile creates a private, Ollama-compatible Cloud Run service
with one L4 GPU per instance.

The root expects an immutable Artifact Registry image with the approved model
artifact already baked in. It does not download a model at startup. The image
must be in the same project and region and must be pinned by digest. A dedicated
runtime service account is created for the model service; only the supplied
worker identity receives `roles/run.invoker`.

Before enabling the paid profile, complete the model evaluation, license and
data-terms review, regional GPU quota check, load test, and cost approval. The
repository does not choose production capacity. Set the warm floor, per-cell
ceiling, and request concurrency in the private variables file from measured
traffic, latency and availability targets, regional quota, and the approved
FinOps model. The assessment cell's capacity and budget do not apply here.

Copy `terraform.tfvars.example` outside Git, replace every placeholder, and
replace the three null capacity fields, and review the plan:

```bash
./task-03-deployment-strategy/scripts/model_plane.sh plan \
  /absolute/path/to/model-plane.tfvars \
  PRODUCTION_TERRAFORM_STATE_BUCKET
```

Apply requires a deliberate cost acknowledgement:

```bash
export MODEL_PLANE_COST_ACKNOWLEDGEMENT=I_ACCEPT_GPU_COSTS
./task-03-deployment-strategy/scripts/model_plane.sh apply \
  /absolute/path/to/model-plane.tfvars \
  PRODUCTION_TERRAFORM_STATE_BUCKET
```

The script uses Terraform only. Local runs use Application Default Credentials;
CI should supply a short-lived Workload Identity credential. The current
assessment container accepts only `fake` and `disabled` inference. A production
release must connect the worker through the regional model gateway and the
existing Ollama provider contract before directing traffic to this service.
