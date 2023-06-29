import logging
import random
import string

import pytest
import yaml
from kubernetes_wrapper import Kubernetes

log = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption(
        "--series",
        type=str,
        default="",
        help="Set series for the machine units",
    )


@pytest.fixture(scope="module")
def k8s_core_bundle(ops_test):
    return ops_test.Bundle("kubernetes-core", channel="edge")


@pytest.fixture(scope="module")
@pytest.mark.asyncio
async def k8s_core_yaml(ops_test, k8s_core_bundle):
    """Download and render the kubernetes-core bundle, return it's full yaml."""
    (bundle_path,) = await ops_test.async_render_bundles(k8s_core_bundle)
    return yaml.safe_load(bundle_path.read_text())


@pytest.fixture(scope="module")
def series(k8s_core_yaml, request):
    series = request.config.getoption("--series")
    return series if series else k8s_core_yaml["series"]


@pytest.fixture(scope="module")
@pytest.mark.asyncio
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
