import logging
import random
import string
from pathlib import Path

import pytest
import yaml
from kubernetes_wrapper import Kubernetes

log = logging.getLogger(__name__)

SERIES_TO_BASE = {
    "focal": "ubuntu@20.04",
    "jammy": "ubuntu@22.04",
    "noble": "ubuntu@24.04",
}
BASE_TO_SERIES = {base: series for series, base in SERIES_TO_BASE.items()}


def series_from_base(base):
    """Return the Ubuntu series name for a Charmhub base string."""
    try:
        return BASE_TO_SERIES[base]
    except KeyError as exc:
        raise ValueError(f"Unsupported base override: {base!r}") from exc


def base_from_series(series):
    """Return the Charmhub base string for an Ubuntu series."""
    try:
        return SERIES_TO_BASE[series]
    except KeyError as exc:
        raise ValueError(f"Unsupported series override: {series!r}") from exc


def resolve_bundle_deployment_target(
    k8s_core_yaml, default_base_override="", series_override=""
):
    """Choose the overlay format and values required for the current bundle."""
    if default_base_override and series_override:
        raise ValueError("Only one of --default-base or --series may be set")

    uses_default_base = "default-base" in k8s_core_yaml
    uses_series = "series" in k8s_core_yaml
    if not uses_default_base and not uses_series:
        raise KeyError("kubernetes-core bundle is missing both 'default-base' and 'series'")

    if default_base_override:
        base = default_base_override
        series = series_from_base(default_base_override)
    elif series_override:
        series = series_override
        base = base_from_series(series_override)
    elif uses_default_base:
        base = k8s_core_yaml["default-base"]
        series = series_from_base(base)
    else:
        series = k8s_core_yaml["series"]
        base = base_from_series(series)

    overlay = "charm.yaml" if uses_default_base else "charm-series.yaml"
    return {
        "base": base,
        "series": series,
        "overlay": Path("tests/data") / overlay,
    }


def pytest_addoption(parser):
    parser.addoption(
        "--default-base",
        type=str,
        default="",
        help="Set default-base for the machine units",
    )
    parser.addoption(
        "--series",
        type=str,
        default="",
        help="Set series for the machine units (deprecated alias for --default-base)",
    )


@pytest.fixture(scope="module")
def k8s_core_bundle(ops_test):
    return ops_test.Bundle("kubernetes-core", channel="edge")


@pytest.fixture(scope="module")
async def k8s_core_yaml(ops_test, k8s_core_bundle):
    """Download and render the kubernetes-core bundle, return it's full yaml."""
    (bundle_path,) = await ops_test.async_render_bundles(k8s_core_bundle)
    return yaml.safe_load(bundle_path.read_text())


@pytest.fixture(scope="module")
def bundle_deployment_target(k8s_core_yaml, request):
    return resolve_bundle_deployment_target(
        k8s_core_yaml,
        default_base_override=request.config.getoption("--default-base"),
        series_override=request.config.getoption("--series"),
    )


@pytest.fixture(scope="module")
def base(bundle_deployment_target):
    return bundle_deployment_target["base"]


@pytest.fixture(scope="module")
def series(bundle_deployment_target):
    return bundle_deployment_target["series"]


@pytest.fixture(scope="module")
def charm_bundle_overlay(bundle_deployment_target):
    return bundle_deployment_target["overlay"]


@pytest.fixture(scope="module")
async def kubernetes(ops_test):
    k_c_p = ops_test.model.applications["kubernetes-control-plane"]
    (leader,) = [u for u in k_c_p.units if (await u.is_leader_from_status())]
    action = await leader.run_action("get-kubeconfig")
    action = await action.wait()
    success = (
        action.status == "completed"
        and action.results["return-code"] == 0
        and "kubeconfig" in action.results
    )

    if not success:
        log.error(f"status: {action.status}")
        log.error(f"results:\n{yaml.safe_dump(action.results, indent=2)}")
        pytest.fail("Failed to copy kubeconfig from kubernetes-control-plane")

    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    with kubeconfig_path.open("w") as f:
        f.write(action.results["kubeconfig"])

    namespace = "test-calico-integration-" + "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(5)
    )
    kubernetes = Kubernetes(namespace, kubeconfig=str(kubeconfig_path))
    namespace_object = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}
    kubernetes.apply_object(namespace_object)
    yield kubernetes
    kubernetes.delete_object(namespace_object)
