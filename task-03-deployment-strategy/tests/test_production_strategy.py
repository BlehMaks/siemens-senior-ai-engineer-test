from __future__ import annotations

import re
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = TASK_ROOT / "architecture" / "production-scale.md"


def _production() -> str:
    return PRODUCTION.read_text(encoding="utf-8")


def _normalized(source: str) -> str:
    return " ".join(source.split())


def test_production_addendum_is_linked_and_local_links_resolve() -> None:
    readme = (TASK_ROOT / "README.md").read_text(encoding="utf-8")
    source = _production()

    assert "architecture/production-scale.md" in readme
    for target in re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", source):
        assert (PRODUCTION.parent / target).resolve().is_file(), target


def test_migration_has_evidence_gates_from_assessment_to_global() -> None:
    source = _production()
    normalized = _normalized(source)

    for phase in (
        "Assessment cell",
        "Corporate pilot",
        "Department rollout",
        "Regional cell",
        "Global governed rollout",
    ):
        assert f"| {phase} |" in source
    for invariant in (
        "tenant authorization",
        "durable accept-before-dispatch",
        "idempotent work",
        "bounded retries",
        "deletion",
        "immutable promotion",
    ):
        assert invariant in normalized


def test_runtime_data_edge_and_recovery_promotions_are_conditional() -> None:
    source = _production()

    for decision in (
        "Cloud Run service",
        "Cloud Run job",
        "Cloud Run worker pool",
        "Cloud Run GPU",
        "GKE Autopilot",
        "GKE Standard",
        "Vertex AI",
        "Firestore",
        "Spanner",
        "AlloyDB/PostgreSQL",
        "HTTPS LB + Cloud Armor",
        "Apigee",
        "Single-region recovery",
        "Active-active cells",
    ):
        assert f"| {decision} |" in source
    assert "company size alone is not a trigger" in source
    assert "provider re-score" in source


def test_slo_residency_iam_and_finops_reviews_have_named_owners() -> None:
    source = _production()

    for heading in (
        "## SLO and disaster-recovery contract",
        "## Tenant and data-residency matrix",
        "## Identity and access review",
        "## FinOps unit-cost worksheet",
        "## Approval ledger and validation checklist",
    ):
        assert heading in source
    for owner in (
        "Product owner",
        "privacy/legal",
        "IAM/platform security",
        "Network + product/OT security",
        "Model risk + legal/procurement",
        "SRE + cell operator",
        "FinOps + product owner",
        "Enterprise architecture + procurement",
    ):
        assert owner in source
    assert "quality-adjusted cost" in source
    assert "RTO" in source and "RPO" in source
    assert "multi-region runtime/data topology" in source
    assert "quota grant or increase" in source


def test_addendum_keeps_enterprise_components_as_explicit_non_claims() -> None:
    source = _production()
    non_claims = _normalized(source.split("## Explicit non-claims", maxsplit=1)[1])

    for component in (
        "Apigee",
        "Spanner",
        "AlloyDB",
        "Pub/Sub",
        "GKE",
        "Vertex AI",
        "Cloud Run GPU",
        "active-active",
        "corporate IdP",
        "OT integration",
    ):
        assert component in non_claims


def test_assessment_capacity_does_not_leak_into_production_model_plane() -> None:
    repo_root = TASK_ROOT.parent
    production_files = [
        repo_root / "docs" / "cloud-resource-manifest.md",
        repo_root / "docs" / "release-and-operations.md",
        PRODUCTION,
        TASK_ROOT
        / "terraform"
        / "environments"
        / "production-model-plane"
        / "README.md",
        TASK_ROOT
        / "terraform"
        / "environments"
        / "production-model-plane"
        / "terraform.tfvars.example",
    ]
    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in production_files
    )

    assert "bounded `min=0`, `max=1` defaults" not in production_text
    assert "model_min_instances            = 0" not in production_text
    assert "model_max_instances            = 1" not in production_text
    assert "one request per instance" not in production_text
