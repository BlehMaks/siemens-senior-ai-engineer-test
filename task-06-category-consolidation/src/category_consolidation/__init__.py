"""Leakage-safe rare-category consolidation for training and inference."""

from .core import (
    MISSING_CATEGORY,
    RareCategoryConsolidator,
    TransformDiagnostics,
    TransformResult,
    UnhashableCategoryError,
    consolidate_rare_categories,
)

__all__ = [
    "MISSING_CATEGORY",
    "RareCategoryConsolidator",
    "TransformDiagnostics",
    "TransformResult",
    "UnhashableCategoryError",
    "consolidate_rare_categories",
]
