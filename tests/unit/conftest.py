import contextlib
import unittest.mock as mock

import ops.testing
import pytest
from ops.testing import Harness

from charm import CalicoCharm

ops.testing.SIMULATE_CAN_CONNECT = True


def pytest_configure(config):
    markers = {
        "skip_install_calico_binaries": "mark tests which do not mock out _install_calico_binaries",
        "skip_get_cni_config": "mark tests which do not mock out _get_cni_config",
    }
    for marker, description in markers.items():
        config.addinivalue_line("markers", f"{marker}: {description}")


@pytest.fixture
def harness():
    harness = Harness(CalicoCharm)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture
def charm(request, harness: Harness[CalicoCharm]):
    """Create a charm with mocked methods.

    This fixture utilizes ExitStack to dynamically mock methods in the Calico Charm,
    using the request markers defined in the `pytest_configure` method.
    """
    with contextlib.ExitStack() as stack:
        methods_to_mock = {
            "_install_calico_binaries": "skip_install_calico_resources",
            "_get_cni_config": "skip_get_cni_config",
        }
        for method, marker in methods_to_mock.items():
            if marker not in request.keywords:
                stack.enter_context(mock.patch(f"charm.CalicoCharm.{method}", mock.MagicMock()))

        harness.begin_with_initial_hooks()
        yield harness.charm


@pytest.fixture(autouse=True)
def lk_client():
    with mock.patch("ops.manifests.manifest.Client", autospec=True) as mock_lightkube:
        yield mock_lightkube.return_value


@pytest.fixture(autouse=True)
def conctl():
    with mock.patch("charm.getContainerRuntimeCtl", autospec=True) as mock_conctl:
        yield mock_conctl.return_value
