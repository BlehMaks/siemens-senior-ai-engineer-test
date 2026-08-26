from importlib import import_module

import pytest

PACKAGES = (
    "search_agent",
    "agent_api",
    "deployment_strategy",
    "binary_classification",
    "material_similarity",
    "category_consolidation",
)


@pytest.mark.parametrize("package", PACKAGES)
def test_workspace_package_imports(package: str) -> None:
    assert import_module(package).__name__ == package
