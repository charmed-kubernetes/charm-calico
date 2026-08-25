import importlib.util
import sys
import types
from pathlib import Path

import pytest


def load_integration_conftest():
    path = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"
    spec = importlib.util.spec_from_file_location("integration_conftest", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    kubernetes_wrapper = types.ModuleType("kubernetes_wrapper")
    kubernetes_wrapper.Kubernetes = type("Kubernetes", (), {})
    sys.modules.setdefault("kubernetes_wrapper", kubernetes_wrapper)
    spec.loader.exec_module(module)
    return module


conftest = load_integration_conftest()


@pytest.mark.parametrize(
    ("bundle_yaml", "expected"),
    [
        (
            {"default-base": "ubuntu@24.04"},
            {
                "base": "ubuntu@24.04",
                "series": "noble",
                "overlay": Path("tests/data/charm.yaml"),
            },
        ),
        (
            {"series": "noble"},
            {
                "base": "ubuntu@24.04",
                "series": "noble",
                "overlay": Path("tests/data/charm-series.yaml"),
            },
        ),
    ],
)
def test_resolve_bundle_deployment_target_uses_bundle_format(bundle_yaml, expected):
    assert conftest.resolve_bundle_deployment_target(bundle_yaml) == expected


def test_resolve_bundle_deployment_target_converts_default_base_override_for_series_bundle():
    assert conftest.resolve_bundle_deployment_target(
        {"series": "noble"}, default_base_override="ubuntu@22.04"
    ) == {
        "base": "ubuntu@22.04",
        "series": "jammy",
        "overlay": Path("tests/data/charm-series.yaml"),
    }


def test_resolve_bundle_deployment_target_converts_series_override_for_default_base_bundle():
    assert conftest.resolve_bundle_deployment_target(
        {"default-base": "ubuntu@24.04"}, series_override="jammy"
    ) == {
        "base": "ubuntu@22.04",
        "series": "jammy",
        "overlay": Path("tests/data/charm.yaml"),
    }


def test_resolve_bundle_deployment_target_rejects_conflicting_overrides():
    with pytest.raises(ValueError, match="Only one of --default-base or --series may be set"):
        conftest.resolve_bundle_deployment_target(
            {"series": "noble"},
            default_base_override="ubuntu@24.04",
            series_override="noble",
        )


def test_resolve_bundle_deployment_target_requires_supported_bundle_key():
    with pytest.raises(
        KeyError, match="kubernetes-core bundle is missing both 'default-base' and 'series'"
    ):
        conftest.resolve_bundle_deployment_target({})


@pytest.mark.parametrize(
    ("func", "value", "message"),
    [
        (conftest.base_from_series, "oracular", "Unsupported series override"),
        (conftest.series_from_base, "ubuntu@26.04", "Unsupported base override"),
    ],
)
def test_bundle_conversion_helpers_raise_for_unknown_values(func, value, message):
    with pytest.raises(ValueError, match=message):
        func(value)
