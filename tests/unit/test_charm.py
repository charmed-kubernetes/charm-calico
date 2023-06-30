# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import os
import unittest.mock as mock
from ipaddress import ip_network
from subprocess import CalledProcessError
from typing import Optional

import ops
import ops.testing
import pytest
from charm import CalicoCharm
from ops.manifests import ManifestClientError
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness
from yaml import YAMLError

ops.testing.SIMULATE_CAN_CONNECT = True


class NetworkMock:
    def __init__(self, version):
        self.version = version


@pytest.mark.parametrize(
    "deployed,side_effect",
    [
        pytest.param(True, None, id="Calico deployed"),
        pytest.param(False, None, id="Calico not deployed"),
        pytest.param(True, ManifestClientError(), id="Manifest exception handled"),
    ],
)
def test_on_etcd_changed(
    harness: Harness, charm: CalicoCharm, caplog, deployed: bool, side_effect: Exception
):
    with mock.patch.object(charm.calico_manifests, "apply_manifests") as mock_apply:
        mock_event = mock.MagicMock()
        mock_apply.side_effect = side_effect
        charm.stored.deployed = deployed
        charm._on_etcd_changed(mock_event)
        if deployed:
            if side_effect:
                mock_event.defer.assert_called_once()
                assert "Failed to update etcd secrets." in caplog.text
            mock_apply.assert_called_once()
        else:
            mock_apply.assert_not_called()


@pytest.mark.parametrize(
    "deployed,side_effect",
    [
        pytest.param(True, None, id="Calico deployed"),
        pytest.param(False, None, id="Calico not deployed"),
        pytest.param(True, CalledProcessError(1, "foo"), id="CalicoCTL unavailable"),
        pytest.param(True, YAMLError(), id="Configuration error"),
    ],
)
@mock.patch("charm.CalicoCharm._configure_calico")
def test_on_config_changed(
    mock_configure: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    deployed: bool,
    side_effect: Exception,
):
    harness.disable_hooks()
    charm.stored.deployed = deployed
    mock_event = mock.MagicMock()
    mock_configure.side_effect = side_effect
    charm._on_config_changed(mock_event)
    if deployed:
        if side_effect:
            if isinstance(side_effect, CalledProcessError):
                mock_event.defer.assert_called_once()
            if isinstance(side_effect, YAMLError):
                assert charm.unit.status == BlockedStatus(
                    "Invalid Config provided. Please check juju debug-log for more info."
                )
        mock_configure.assert_called_once()
    else:
        mock_configure.assert_not_called()


@mock.patch("charm.CalicoCharm._configure_calico_pool")
@mock.patch("charm.CalicoCharm._configure_node")
@mock.patch("charm.CalicoCharm._configure_bgp_globals")
@mock.patch("charm.CalicoCharm._configure_bgp_peers")
@mock.patch("charm.CalicoCharm._disable_vxlan_tx_checksumming")
def test_configure_calico(
    mock_config_pool: mock.MagicMock,
    mock_config_node: mock.MagicMock,
    mock_bgp_globals: mock.MagicMock,
    mock_bgp_peers: mock.MagicMock,
    mock_vxlan: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
):
    harness.disable_hooks()
    charm._configure_calico()
    mock_config_pool.assert_called_once()
    mock_config_node.assert_called_once()
    mock_bgp_globals.assert_called_once()
    mock_bgp_peers.assert_called_once()
    mock_vxlan.assert_called_once()


@mock.patch("charm.CalicoCharm._configure_calico_pool")
def test_configure_calico_exception_handling(mock_configure: mock.MagicMock, charm: CalicoCharm):
    mock_configure.side_effect = YAMLError()
    with pytest.raises(YAMLError):
        charm._configure_calico()
    assert not charm.stored.calico_configured


@mock.patch("charm.CalicoCharm._install_or_upgrade")
@mock.patch("charm.CalicoCharm._remove_calico_reactive")
def test_on_upgrade(
    mock_remove: mock.MagicMock, mock_install: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    harness.disable_hooks()
    mock_event = mock.MagicMock()
    charm._on_upgrade(mock_event)
    mock_remove.assert_called_once()
    mock_install.assert_called_once_with(mock_event)


def test_get_kubeconfig_status(harness, charm):
    harness.disable_hooks()
    rel_id = harness.add_relation("cni", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")
    assert not charm._get_kubeconfig_status()

    harness.update_relation_data(
        rel_id, "kubernetes-control-plane/0", {"kubeconfig-hash": "abcd1234"}
    )
    assert charm._get_kubeconfig_status()


@pytest.mark.parametrize(
    "ready",
    [
        pytest.param(True, id="Calico ready"),
        pytest.param(False, id="Calico not ready"),
    ],
)
def test_set_status(charm: CalicoCharm, ready: bool):
    with mock.patch.object(charm.unit, "set_workload_version") as mock_set:
        charm.stored.deployed = ready
        charm.stored.calico_configured = ready
        charm._set_status()
        if ready:
            mock_set.assert_called_once()
            assert charm.unit.status == ActiveStatus("Ready")
        else:
            mock_set.assert_not_called()


def test_on_etcd_connected(charm: CalicoCharm):
    mock_event = mock.MagicMock()
    charm._on_etcd_connected(mock_event)
    assert charm.unit.status == BlockedStatus("Waiting for relation to etcd.")


@pytest.mark.parametrize(
    "mtu,ipip,vxlan,result",
    [
        pytest.param(1500, "Never", "Always", 1450, id="VXLAN MTU"),
        pytest.param(1500, "Always", "Never", 1480, id="IPIP MTU"),
        pytest.param(None, "Never", "Never", None, id="No MTU provided"),
        pytest.param(1400, "Never", "Never", 1400, id="Custom MTU provided"),
    ],
)
def test_get_mtu(
    harness: Harness, charm: CalicoCharm, mtu: int, ipip: str, vxlan: str, result: Optional[int]
):
    harness.update_config({"veth-mtu": mtu, "ipip": ipip, "vxlan": vxlan})
    mtu = charm._get_mtu()
    assert mtu == result


@pytest.mark.parametrize(
    "detected",
    [
        pytest.param(True, id="Upgrading from reactive"),
        pytest.param(False, id="Upgrading from ops"),
    ],
)
@mock.patch("charm.service_running")
@mock.patch("charm.service_stop")
@mock.patch("charm.daemon_reload")
@mock.patch("os.path.isfile")
@mock.patch("os.remove")
def test_remove_calico_reactive(
    mock_remove: mock.MagicMock,
    mock_isfile: mock.MagicMock,
    mock_reload: mock.MagicMock,
    mock_stop: mock.MagicMock,
    mock_running: mock.MagicMock,
    charm: CalicoCharm,
    caplog,
    detected: bool,
):
    service_path = os.path.join(os.sep, "lib", "systemd", "system", "calico-node.service")
    cni_bin_path = "/opt/cni/bin/"
    mocks = (mock_running, mock_stop, mock_reload, mock_isfile)

    for mock_obj in mocks:
        mock_obj.return_value = detected

    charm._remove_calico_reactive()

    mock_running.assert_called_once_with("calico-node")
    mock_isfile.assert_has_calls(
        [
            mock.call(service_path),
            mock.call(cni_bin_path + "calico"),
            mock.call(cni_bin_path + "calico-ipam"),
        ]
    )

    if detected:
        mock_stop.assert_called_once_with("calico-node")

        mock_remove.assert_has_calls(
            [
                mock.call(service_path),
                mock.call(cni_bin_path + "calico"),
                mock.call(cni_bin_path + "calico-ipam"),
            ]
        )

        assert "calico-node service stopped." in caplog.text
        assert "calico-node service removed and daemon reloaded." in caplog.text
        assert "calico binary uninstalled." in caplog.text
        assert "calico-ipam binary uninstalled." in caplog.text
    else:
        assert "calico-node service successfully stopped." in caplog.text
        assert "calico-node service successfully uninstalled." in caplog.text
        assert "calico binary successfully uninstalled." in caplog.text
        assert "calico-ipam binary successfully uninstalled." in caplog.text


@mock.patch("charm.CalicoCharm._get_networks")
def test_get_ip_versions(mock_get_networks: mock.MagicMock, harness: Harness, charm: CalicoCharm):
    mock_networks = [NetworkMock(version) for version in [4, 6, 4, 6, 4]]
    mock_get_networks.return_value = mock_networks

    result = charm._get_ip_versions()

    assert result == {4, 6}


@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_bgp_globals(
    mock_get: mock.MagicMock, mock_apply: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    harness.update_config(
        {
            "global-as-number": 64511,
            "bgp-service-cluster-ips": "10.0.0.0/16",
            "bgp-service-external-ips": "192.168.0.0/16",
            "bgp-service-loadbalancer-ips": "172.16.0.0/16",
        }
    )
    mock_get.return_value = {
        "apiVersion": "projectcalico.org/v3",
        "kind": "BGPConfiguration",
        "metadata": {"name": "default"},
        "spec": {},
    }

    charm._configure_bgp_globals()
    mock_get.assert_called_once_with("bgpconfig", "default")
    mock_apply.assert_called_once()

    apply_args, _ = mock_apply.call_args
    applied_config = apply_args[0]

    assert applied_config["spec"]["asNumber"] == 64511
    assert applied_config["spec"]["serviceClusterIPs"] == [{"cidr": "10.0.0.0/16"}]
    assert applied_config["spec"]["serviceExternalIPs"] == [{"cidr": "192.168.0.0/16"}]
    assert applied_config["spec"]["serviceLoadBalancerIPs"] == [{"cidr": "172.16.0.0/16"}]


@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_bgp_globals_get_raises(mock_get: mock.MagicMock, charm: CalicoCharm, caplog):
    mock_get.side_effect = CalledProcessError(1, "foo", b"some output", b"some error")

    with pytest.raises(CalledProcessError):
        charm._configure_bgp_globals()
        assert "Failed to get BGPConfiguration" in caplog.text


@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_bgp_globals_apply_raises(
    mock_get: mock.MagicMock,
    mock_apply: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    caplog,
):
    harness.update_config(
        {
            "global-as-number": 64511,
            "bgp-service-cluster-ips": "10.0.0.0/16",
            "bgp-service-external-ips": "192.168.0.0/16",
            "bgp-service-loadbalancer-ips": "172.16.0.0/16",
        }
    )
    mock_get.side_effect = CalledProcessError(1, "foo", b"some output", b"resource does not exist")
    mock_apply.side_effect = CalledProcessError(1, "foo")

    with pytest.raises(CalledProcessError):
        charm._configure_bgp_globals()


@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_bgp_globals_resource(
    mock_get: mock.MagicMock,
    mock_apply: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    caplog,
):
    harness.update_config(
        {
            "global-as-number": 64511,
            "bgp-service-cluster-ips": "10.0.0.0/16",
            "bgp-service-external-ips": "192.168.0.0/16",
            "bgp-service-loadbalancer-ips": "172.16.0.0/16",
        }
    )
    mock_get.side_effect = CalledProcessError(1, "foo", b"some output", b"resource does not exist")

    charm._configure_bgp_globals()
    mock_get.assert_called_once_with("bgpconfig", "default")
    mock_apply.assert_called_once()

    assert "default BGPConfiguration does not exist." in caplog.text


@mock.patch("charm.gethostname")
@mock.patch("charm.CalicoCharm._get_unit_as_number")
@mock.patch("charm.CalicoCharm._get_route_reflector_cluster_id")
@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_node(
    mock_get: mock.MagicMock,
    mock_apply: mock.MagicMock,
    mock_cluster_id: mock.MagicMock,
    mock_unit: mock.MagicMock,
    mock_hostname: mock.MagicMock,
    charm: CalicoCharm,
):
    mock_hostname.return_value = "test-node"
    mock_unit.return_value = 64511
    mock_cluster_id.return_value = "224.0.0.1"
    mock_get.return_value = {
        "apiVersion": "projectcalico.org/v3",
        "kind": "BGPConfiguration",
        "metadata": {"name": "default"},
        "spec": {"bgp": {}},
    }

    charm._configure_node()

    mock_get.assert_called_once_with("node", "test-node")
    mock_apply.assert_called_once()

    apply_args, _ = mock_apply.call_args
    applied_config = apply_args[0]
    assert applied_config["spec"]["bgp"]["asNumber"] == 64511
    assert applied_config["spec"]["bgp"]["routeReflectorClusterID"] == "224.0.0.1"


@mock.patch("charm.CalicoCharm._get_unit_as_number")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_node_raises(
    mock_get: mock.MagicMock, mock_unit: mock.MagicMock, charm: CalicoCharm
):
    mock_get.side_effect = CalledProcessError(1, "foo", b"some output", b"some error")
    mock_unit.return_value = 64511

    with pytest.raises(CalledProcessError):
        charm._configure_node()


@mock.patch("charm.CalicoCharm._filter_local_subnets")
@mock.patch("charm.CalicoCharm._get_unit_id")
def test_get_unit_as_number_unit(
    mock_unit: mock.MagicMock, mock_filter: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    mock_unit.return_value = 0
    harness.update_config({"unit-as-numbers": "{0: 64512, 1: 64513}"})
    result = charm._get_unit_as_number()

    assert result == 64512


@mock.patch("charm.CalicoCharm._filter_local_subnets")
@mock.patch("charm.CalicoCharm._get_unit_id")
def test_get_unit_as_number_subnet(
    mock_unit: mock.MagicMock, mock_filter: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    mock_unit.return_value = 0
    harness.update_config(
        {
            "unit-as-numbers": "{1: 64512, 2: 64513}",
            "subnet-as-numbers": "{10.0.0.0/24: 64515, 10.0.1.0/24: 64513}",
        }
    )
    mock_filter.return_value = [ip_network("10.0.0.0/24")]
    result = charm._get_unit_as_number()

    assert result == 64515


@mock.patch("charm.CalicoCharm._filter_local_subnets")
@mock.patch("charm.CalicoCharm._get_unit_id")
def test_get_unit_as_number_no_as_subnet(
    mock_unit: mock.MagicMock, mock_filter: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    mock_unit.return_value = 0
    mock_filter.return_value = [ip_network("10.0.0.0/24")]
    result = charm._get_unit_as_number()

    assert result is None


@mock.patch("charm.CalicoCharm._filter_local_subnets")
@mock.patch("charm.CalicoCharm._get_unit_id")
def test_get_unit_as_number_none(
    mock_unit: mock.MagicMock, mock_filter: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
    mock_unit.return_value = 0
    mock_filter.return_value = []
    result = charm._get_unit_as_number()

    assert result is None


@mock.patch("charm.CalicoCharm._get_bind_address")
def test_filter_local_subnets(mock_bind: mock.MagicMock, charm: CalicoCharm ):
    mock_bind.return_value = "192.168.1.3"

    subnets = ["192.168.1.0/24", "10.0.0.0/16"]
    result = charm._filter_local_subnets(subnets)
    expected = [ip_network("192.168.1.0/24")]

    assert result == expected
