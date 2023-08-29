# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import ipaddress
import os
import unittest.mock as mock
from asyncio import subprocess
from ipaddress import ip_network
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from typing import Optional, Set

import ops
import ops.testing
import pytest
from charm import CalicoCharm
from ops.manifests import ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, ModelError, WaitingStatus
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
    with mock.patch.object(charm.calico_manifests, "apply_manifests"):
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


@mock.patch("charm.CalicoCharm._set_status")
@mock.patch("charm.CalicoCharm._configure_calico")
@mock.patch("charm.CalicoCharm._configure_cni")
@mock.patch("charm.CalicoCharm._get_kubeconfig_status", return_value=True)
def test_install_or_upgrade(
    mock_kubeconfig: mock.MagicMock,
    mock_cni: mock.MagicMock,
    mock_configure: mock.MagicMock,
    mock_set_status: mock.MagicMock,
    charm: CalicoCharm,
):
    with mock.patch.object(charm, "etcd") as mock_etcd, mock.patch.object(
        charm.calico_manifests, "apply_manifests"
    ) as mock_apply:
        mock_etcd.return_value.is_ready.return_value = True
        mock_event = mock.MagicMock()
        charm._install_or_upgrade(mock_event)
        mock_cni.assert_called_once()
        mock_configure.assert_called_once()
        mock_set_status.assert_called_once()
        mock_apply.assert_called_once()
        assert charm.stored.deployed


@mock.patch("charm.CalicoCharm._set_status")
@mock.patch("charm.CalicoCharm._configure_calico")
@mock.patch("charm.CalicoCharm._configure_cni")
@mock.patch("charm.CalicoCharm._get_kubeconfig_status", return_value=True)
def test_install_or_upgrade_etcd_unavailable(
    mock_kubeconfig: mock.MagicMock,
    mock_cni: mock.MagicMock,
    mock_configure: mock.MagicMock,
    mock_set_status: mock.MagicMock,
    charm: CalicoCharm,
):
    with mock.patch.object(charm, "etcd") as mock_etcd, mock.patch.object(
        charm.calico_manifests, "apply_manifests"
    ):
        mock_etcd.is_ready = False
        mock_event = mock.MagicMock()
        charm._install_or_upgrade(mock_event)
        assert charm.unit.status == BlockedStatus("Waiting for etcd.")
        mock_event.defer.assert_called_once()
        assert not charm.stored.deployed


@mock.patch("charm.CalicoCharm._set_status")
@mock.patch("charm.CalicoCharm._configure_calico")
@mock.patch("charm.CalicoCharm._configure_cni")
@mock.patch("charm.CalicoCharm._get_kubeconfig_status", return_value=True)
def test_install_or_upgrade_config(
    mock_kubeconfig: mock.MagicMock,
    mock_cni: mock.MagicMock,
    mock_configure: mock.MagicMock,
    mock_set_status: mock.MagicMock,
    charm: CalicoCharm,
):
    with mock.patch.object(charm, "etcd") as mock_etcd, mock.patch.object(
        charm.calico_manifests, "apply_manifests"
    ):
        mock_etcd.return_value.is_ready.return_value = True
        mock_event = mock.MagicMock()
        mock_configure.side_effect = YAMLError("foo")
        charm._install_or_upgrade(mock_event)
        assert charm.unit.status == BlockedStatus(
            "Invalid Config provided. Please check juju debug-log for more info."
        )
        mock_event.defer.assert_not_called()
        assert not charm.stored.deployed


@pytest.mark.parametrize(
    "side_effect,status",
    [
        pytest.param(
            ManifestClientError("foo"),
            WaitingStatus("Installing Calico manifests"),
            id="ManifestClientError",
        ),
        pytest.param(
            CalledProcessError(1, "foo"),
            WaitingStatus("Configuring Calico"),
            id="CalledProcessError",
        ),
    ],
)
@mock.patch("charm.CalicoCharm._set_status")
@mock.patch("charm.CalicoCharm._configure_calico")
@mock.patch("charm.CalicoCharm._configure_cni")
@mock.patch("charm.CalicoCharm._get_kubeconfig_status", return_value=True)
def test_install_or_upgrade_exception(
    mock_kubeconfig: mock.MagicMock,
    mock_cni: mock.MagicMock,
    mock_configure: mock.MagicMock,
    mock_set_status: mock.MagicMock,
    charm: CalicoCharm,
    side_effect: Exception,
    status,
):
    with mock.patch.object(charm, "etcd") as mock_etcd, mock.patch.object(
        charm.calico_manifests, "apply_manifests"
    ):
        mock_etcd.return_value.is_ready.return_value = True
        mock_event = mock.MagicMock()
        mock_configure.side_effect = side_effect
        charm._install_or_upgrade(mock_event)
        assert charm.unit.status == status
        mock_event.defer.assert_called_once()
        assert not charm.stored.deployed


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
@mock.patch("charm.CalicoCharm._is_rpf_config_mismatched", return_value=False)
def test_set_status(mock_rpf: mock.MagicMock, charm: CalicoCharm, ready: bool):
    with mock.patch.object(charm.unit, "set_workload_version") as mock_set:
        charm.stored.deployed = ready
        charm.stored.calico_configured = ready
        charm._set_status()
        if ready:
            mock_set.assert_called_once()
            assert charm.unit.status == ActiveStatus("Ready")
        else:
            mock_set.assert_not_called()


@mock.patch("charm.CalicoCharm._is_rpf_config_mismatched", return_value=True)
def test_set_status_rpf_mismatched(mock_rpf: mock.MagicMock, charm: CalicoCharm):
    with mock.patch.object(charm.unit, "set_workload_version"):
        charm._set_status()
        assert charm.unit.status == BlockedStatus(
            "ignore-loose-rpf config is in conflict with rp_filter value"
        )


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
    conctl,
    caplog,
    detected: bool,
):
    service_path = os.path.join(os.sep, "lib", "systemd", "system", "calico-node.service")
    mocks = (mock_running, mock_stop, mock_reload, mock_isfile)

    for mock_obj in mocks:
        mock_obj.return_value = detected

    charm._remove_calico_reactive()

    mock_running.assert_called_once_with("calico-node")
    mock_isfile.assert_has_calls(
        [
            mock.call(service_path),
        ]
    )
    conctl.delete.return_value = CompletedProcess([], 0)
    conctl.delete.assert_called_once_with("calico-node")

    if detected:
        mock_stop.assert_called_once_with("calico-node")

        mock_remove.assert_has_calls(
            [
                mock.call(service_path),
            ]
        )

        assert "calico-node service stopped." in caplog.text
        assert "calico-node service removed and daemon reloaded." in caplog.text
    else:
        assert "calico-node service successfully stopped." in caplog.text
        assert "calico-node service successfully uninstalled." in caplog.text


@mock.patch("charm.CalicoCharm._get_networks")
def test_get_ip_versions(mock_get_networks: mock.MagicMock, harness: Harness, charm: CalicoCharm):
    mock_networks = [NetworkMock(version) for version in [4, 6, 4, 6, 4]]
    mock_get_networks.return_value = mock_networks

    result = charm._get_ip_versions()

    assert result == {4, 6}


def test_get_networks(charm: CalicoCharm):
    cidrs = "192.168.0.0/24,10.0.0.0/16"

    result = charm._get_networks(cidrs)
    expected_result = [ipaddress.ip_network("192.168.0.0/24"), ipaddress.ip_network("10.0.0.0/16")]

    assert result == expected_result


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


@mock.patch("charm.CalicoCharm._filter_local_subnets", return_value=[ip_network("10.0.0.0/24")])
@mock.patch("charm.CalicoCharm._get_unit_id", return_value=0)
def test_get_unit_as_number_no_as_subnet(
    mock_unit: mock.MagicMock, mock_filter: mock.MagicMock, harness: Harness, charm: CalicoCharm
):
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
def test_filter_local_subnets(mock_bind: mock.MagicMock, charm: CalicoCharm):
    mock_bind.return_value = "192.168.1.3"

    subnets = ["192.168.1.0/24", "10.0.0.0/16"]
    result = charm._filter_local_subnets(subnets)
    expected = [ip_network("192.168.1.0/24")]

    assert result == expected


@mock.patch("charm.gethostname", return_value="test-node")
@mock.patch("charm.CalicoCharm.calicoctl")
@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
@mock.patch("charm.CalicoCharm._filter_local_subnets", return_value=[ip_network("10.0.0.0/24")])
@mock.patch("charm.CalicoCharm._get_unit_id", return_value=0)
def test_configure_bgp_peers_unit_peers(
    mock_unit: mock.MagicMock,
    mock_filter: mock.MagicMock,
    mock_get: mock.MagicMock,
    mock_apply: mock.MagicMock,
    mock_calicoctl: mock.MagicMock,
    mock_hostname: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
):
    harness.update_config(
        {
            "unit-bgp-peers": "{0: [{address: 10.0.0.1, as-number: 65000}, {address: 10.0.0.2, as-number: 65001}], 1: [{address: 10.0.1.1, as-number: 65002}]}"
        }
    )
    rogue_def = {
        "items": [
            {
                "apiVersion": "projectcalico.org/v3",
                "kind": "BGPPeer",
                "metadata": {"name": "calico-0-10.20.0.1-65000"},
                "spec": {"node": "test-node", "peerIP": "10.0.0.1", "asNumber": 65000},
            }
        ]
    }
    mock_get.return_value = rogue_def

    charm._configure_bgp_peers()
    mock_calicoctl.assert_called_once_with("delete", "bgppeer", "calico-0-10.20.0.1-65000")
    mock_apply.assert_has_calls(
        [
            mock.call(
                {
                    "apiVersion": "projectcalico.org/v3",
                    "kind": "BGPPeer",
                    "metadata": {"name": "calico-0-10.0.0.1-65000"},
                    "spec": {"node": "test-node", "peerIP": "10.0.0.1", "asNumber": 65000},
                }
            ),
            mock.call(
                {
                    "apiVersion": "projectcalico.org/v3",
                    "kind": "BGPPeer",
                    "metadata": {"name": "calico-0-10.0.0.2-65001"},
                    "spec": {"node": "test-node", "peerIP": "10.0.0.2", "asNumber": 65001},
                }
            ),
        ]
    )


@mock.patch("charm.gethostname", return_value="test-node")
@mock.patch("charm.CalicoCharm.calicoctl")
@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm._calicoctl_get")
@mock.patch("charm.CalicoCharm._filter_local_subnets", return_value=[ip_network("10.0.0.0/24")])
@mock.patch("charm.CalicoCharm._get_unit_id", return_value=0)
def test_configure_bgp_peers_raises(
    mock_unit: mock.MagicMock,
    mock_filter: mock.MagicMock,
    mock_get: mock.MagicMock,
    mock_apply: mock.MagicMock,
    mock_calicoctl: mock.MagicMock,
    mock_hostname: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    caplog,
):
    harness.update_config(
        {
            "unit-bgp-peers": "{0: [{address: 10.0.0.1, as-number: 65000}, {address: 10.0.0.2, as-number: 65001}], 1: [{address: 10.0.1.1, as-number: 65002}]}"
        }
    )
    mock_apply.side_effect = CalledProcessError(1, "foo", "some output", "some error")
    with pytest.raises(CalledProcessError):
        charm._configure_bgp_peers()
        assert "Failed to apply BGP peer configuration." in caplog.text


@pytest.mark.parametrize(
    "ip_versions,expected_config",
    [
        pytest.param(
            {4, 6},
            {
                "kubeconfig_path": "/opt/calicoctl/kubeconfig",
                "mtu": 1500,
                "assign_ipv4": "true",
                "assign_ipv6": "true",
                "IP6": "autodetect",
            },
            id="Dualstack",
        ),
        pytest.param(
            {4},
            {
                "kubeconfig_path": "/opt/calicoctl/kubeconfig",
                "mtu": 1500,
                "assign_ipv4": "true",
                "assign_ipv6": "false",
                "IP6": "none",
            },
            id="Singlestack",
        ),
    ],
)
@mock.patch("charm.CalicoCharm._propagate_cni_config")
@mock.patch("charm.CalicoCharm._get_ip_versions")
@mock.patch("charm.CalicoCharm._get_mtu", return_value=1500)
def test_configure_cni(
    mock_mtu: mock.MagicMock,
    mock_get_ip: mock.MagicMock,
    mock_propagate: mock.MagicMock,
    charm: CalicoCharm,
    ip_versions: Set,
    expected_config: dict,
):
    charm.stored.cni_configured = False
    mock_get_ip.return_value = ip_versions

    charm._configure_cni()

    assert charm.cni_config == expected_config
    assert charm.stored.cni_configured
    mock_mtu.assert_called_once()
    mock_propagate.assert_called_once()


@pytest.mark.parametrize(
    "disable,vxlan",
    [
        pytest.param(True, "Always", id="Disable VXLAN TX checksumming"),
        pytest.param(False, "Never", id="Don't disable VXLAN TX checksumming"),
    ],
)
@mock.patch("subprocess.check_call")
def test_disable_vxlan_tx_checksumming(
    mock_check_call: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    disable: bool,
    vxlan: str,
):
    harness.update_config({"disable-vxlan-tx-checksumming": disable, "vxlan": vxlan})
    charm._disable_vxlan_tx_checksumming()
    if disable:
        mock_check_call.assert_called_once_with(
            ["ethtool", "-K", "vxlan.calico", "tx-checksum-ip-generic", "off"]
        )
    else:
        mock_check_call.assert_not_called()


@mock.patch("subprocess.check_call", side_effect=(CalledProcessError(1, "ethtool")))
def test_disable_vxlan_tx_checksumming_raises(
    mock_check_call: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
):
    harness.update_config({"disable-vxlan-tx-checksumming": True, "vxlan": "Always"})
    with pytest.raises(CalledProcessError):
        charm._disable_vxlan_tx_checksumming()
    mock_check_call.assert_called_once_with(
        ["ethtool", "-K", "vxlan.calico", "tx-checksum-ip-generic", "off"]
    )


def test_propagate_cni_config(harness: Harness, charm: CalicoCharm):
    harness.disable_hooks()
    config_dict = {"cidr": "10.0.0.0/24"}
    harness.update_config(config_dict)
    rel_id = harness.add_relation("cni", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")

    charm._propagate_cni_config()
    assert len(harness.model.relations["cni"]) == 1
    relation = harness.model.relations["cni"][0]
    assert relation.data[charm.unit] == {
        "cidr": "10.0.0.0/24",
        "cni-conf-file": "10-calico.conflist",
    }


@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_calico_pool_unmanaged(
    mock_get: mock.MagicMock, harness: Harness, charm: CalicoCharm, caplog
):
    harness.update_config({"manage-pools": False})
    charm._configure_calico_pool()
    mock_get.assert_not_called()
    assert "Skipping pool configuration." in caplog.text


@mock.patch("charm.CalicoCharm._calicoctl_apply")
@mock.patch("charm.CalicoCharm.calicoctl")
@mock.patch("charm.CalicoCharm._calicoctl_get")
def test_configure_calico_pool(
    mock_get: mock.MagicMock,
    mock_calicoctl: mock.MagicMock,
    mock_apply: mock.MagicMock,
    harness: Harness,
    charm: CalicoCharm,
    caplog,
):
    harness.update_config(
        {
            "manage-pools": True,
            "cidr": "192.0.2.0/24",
            "ipip": "Always",
            "vxlan": "Never",
            "nat-outgoing": True,
        }
    )

    mock_get.return_value = {
        "items": [
            {
                "apiVersion": "projectcalico.org/v3",
                "kind": "IPPool",
                "metadata": {"name": "intergalactic"},
                "spec": {
                    "cidr": "10.0.1.0/24",
                    "ipipMode": "Always",
                    "vxlanMode": "Never",
                    "natOutgoing": True,
                },
            },
        ]
    }
    charm._configure_calico_pool()
    mock_get.assert_called_once_with("pool")
    mock_calicoctl.assert_called_once_with("delete", "pool", "intergalactic", "--skip-not-exists")
    mock_apply.assert_called_once_with(
        {
            "apiVersion": "projectcalico.org/v3",
            "kind": "IPPool",
            "metadata": {"name": "ipv4"},
            "spec": {
                "cidr": "192.0.2.0/24",
                "ipipMode": "Always",
                "vxlanMode": "Never",
                "natOutgoing": True,
            },
        }
    )


@mock.patch("charm.CalicoCharm._calicoctl_get", side_effect=CalledProcessError(1, "foo"))
def test_configure_calico_pool_raises(
    mock_get: mock.MagicMock, harness: Harness, charm: CalicoCharm, caplog
):
    harness.update_config({"manage-pools": True})
    with pytest.raises(CalledProcessError):
        charm._configure_calico_pool()
    assert "Failed to modify IP Pools." in caplog.text


@pytest.mark.parametrize(
    "side_effect,expected_status",
    [
        pytest.param(ModelError(), BlockedStatus("Error claiming calico"), id="Model Error"),
        pytest.param(NameError(), BlockedStatus("Resource calico not found"), id="Name Error"),
    ],
)
def test_install_calico_resources_exception(
    harness: Harness, side_effect: Exception, expected_status
):
    harness.disable_hooks()
    harness.begin()
    charm = harness.charm
    with mock.patch.object(charm.model.resources, "fetch") as mock_fetch:
        mock_fetch.side_effect = side_effect

        charm._install_calico_binaries()

        assert charm.unit.status == expected_status


@mock.patch("shutil.copy")
@mock.patch("subprocess.check_call")
@mock.patch("charm.CalicoCharm._unpack_archive")
@mock.patch("os.chmod")
@mock.patch("os.stat")
def test_install_calico_resources(
    mock_stat: mock.MagicMock,
    mock_chmod: mock.MagicMock,
    mock_unpack: mock.MagicMock,
    mock_check: mock.MagicMock,
    mock_copy: mock.MagicMock,
    harness: Harness,
):
    harness.disable_hooks()
    harness.begin()
    charm = harness.charm
    with mock.patch.object(charm.model.resources, "fetch") as mock_fetch:
        mock_fetch.return_value = "/path/to/resource"
        mock_stat.return_value.st_size = 2000000

        charm._install_calico_binaries()

        mock_fetch.assert_called_once_with("calico")
        mock_stat.assert_called_once_with("/path/to/resource")
        mock_unpack.assert_called_once()
        mock_check.assert_called_once()
        mock_chmod.assert_called_once_with("/usr/local/bin/calicoctl", 0o755)
        mock_copy.assert_called_once_with(Path("./scripts/calicoctl"), "/usr/local/bin/calicoctl")
        assert charm.stored.binaries_installed


@mock.patch("shutil.copy")
@mock.patch("subprocess.check_call")
@mock.patch("charm.CalicoCharm._unpack_archive")
@mock.patch("os.chmod")
@mock.patch("os.stat")
def test_install_calico_resources_raises(
    mock_stat: mock.MagicMock,
    mock_chmod: mock.MagicMock,
    mock_unpack: mock.MagicMock,
    mock_check: mock.MagicMock,
    mock_copy: mock.MagicMock,
    harness: Harness,
    caplog,
):
    harness.disable_hooks()
    harness.begin()
    charm = harness.charm
    with mock.patch.object(charm.model.resources, "fetch") as mock_fetch:
        mock_fetch.return_value = "/path/to/resource"
        mock_stat.return_value.st_size = 2000000
        mock_check.side_effect = CalledProcessError(1, "cmd", "some output", "some error")

        charm._install_calico_binaries()
        assert charm.unit.status == BlockedStatus("Failed to install calicoctl")
        assert "Failed to install calicoctl" in caplog.text


@mock.patch("os.stat")
def test_install_calico_resources_filesize(
    mock_stat: mock.MagicMock,
    harness: Harness,
):
    harness.disable_hooks()
    harness.begin()
    charm = harness.charm
    with mock.patch.object(charm.model.resources, "fetch") as mock_fetch:
        mock_fetch.return_value = "/path/to/resource"
        mock_stat.return_value.st_size = 500

        charm._install_calico_binaries()
        assert charm.unit.status == BlockedStatus("Incomplete resource: calico")


@mock.patch("charm.CalicoCharm._get_calicoctl_env")
@mock.patch("pathlib.Path.mkdir")
@mock.patch("pathlib.Path.open")
def test_update_calicoctl_env(
    mock_open: mock.MagicMock,
    mock_path: mock.MagicMock,
    mock_get_env: mock.MagicMock,
    charm: CalicoCharm,
):
    env = {
        "ETCD_ENDPOINTS": "/foo/path/endpoints",
        "ETCD_KEY_FILE": "/foo/path/key",
        "ETCD_CERT_FILE": "/foo/path/cert",
        "ETCD_CA_CERT_FILE": "/foo/path/ca",
    }
    mock_get_env.return_value = env
    mock_event = mock.MagicMock()
    charm._update_calicoctl_env(mock_event)

    mock_path.assert_called_once_with(parents=True, exist_ok=True)
    mock_open.assert_called_once_with("w")
    handle = mock_open().__enter__()
    handle.write.assert_called_once_with(
        "export ETCD_CA_CERT_FILE=/foo/path/ca\nexport ETCD_CERT_FILE=/foo/path/cert\nexport ETCD_ENDPOINTS=/foo/path/endpoints\nexport ETCD_KEY_FILE=/foo/path/key"
    )


@mock.patch(
    "charms.kubernetes_libs.v0.etcd.EtcdReactiveRequires.get_connection_string",
    return_value="https://10.0.10.24:4343",
)
def test_get_calicoctl_env(mock_etcd: mock.PropertyMock, charm: CalicoCharm):
    expected_env = {
        "ETCD_ENDPOINTS": "https://10.0.10.24:4343",
        "ETCD_KEY_FILE": "/opt/calicoctl/etcd-key",
        "ETCD_CERT_FILE": "/opt/calicoctl/etcd-cert",
        "ETCD_CA_CERT_FILE": "/opt/calicoctl/etcd-ca",
    }

    result = charm._get_calicoctl_env()
    assert expected_env == result


@mock.patch("tarfile.open")
def test_unpack_archive(mock_tarfile_open: mock.MagicMock, charm: CalicoCharm):
    source_path = "/test/path"
    dst_path = "/dst/path"

    charm._unpack_archive(source_path, dst_path)
    mock_tarfile_open.assert_called_once_with(source_path)
    mock_tarfile_open().extractall.assert_called_once_with(dst_path)


@mock.patch("charm.CalicoCharm.calicoctl")
@mock.patch("tempfile.TemporaryDirectory")
@mock.patch("builtins.open")
def test_calicoctl_apply(
    mock_open: mock.MagicMock,
    mock_tempdir: mock.MagicMock,
    mock_calicoctl: mock.MagicMock,
    charm: CalicoCharm,
):
    test_data = {"key": "value"}
    mock_tempdir.return_value.__enter__.return_value = "/tmp/dir"
    charm._calicoctl_apply(test_data)

    mock_tempdir.assert_called_once()
    filename = "/tmp/dir/calicoctl_manifest.yaml"
    mock_open.assert_called_once_with(filename, "w")
    mock_calicoctl.assert_called_once_with("apply", "-f", filename)


@mock.patch("charm.CalicoCharm.calicoctl")
def test_calicoctl_get(mock_calicoctl: mock.MagicMock, charm: CalicoCharm):
    test_args = ("node", "juju-a43756-1")
    expected_args = ("get", "-o", "yaml", "--export") + test_args
    expected_dict = {"key": "value"}
    mock_calicoctl.return_value = "key: value"
    result = charm._calicoctl_get(*test_args)

    mock_calicoctl.assert_called_once_with(*expected_args)
    assert result == expected_dict


@mock.patch("charm.CalicoCharm.calicoctl")
def test_calicoctl_get_raises(mock_calicoctl: mock.MagicMock, charm: CalicoCharm, caplog):
    test_args = ("node", "juju-a43756-1")
    mock_calicoctl.return_value = 'key: - "value2 : a'
    with pytest.raises(YAMLError):
        charm._calicoctl_get(*test_args)
    assert "Failed to parse calicoctl output as yaml" in caplog.text


@mock.patch("subprocess.check_output")
@mock.patch("charm.CalicoCharm._get_calicoctl_env")
def test_calicoctl(mock_get: mock.MagicMock, mock_check: mock.MagicMock, charm: CalicoCharm):
    test_args = ("get", "version")
    mock_get.return_value = {"ETCD_KEY_FILE": "/tmp/test/path/key"}
    expected_cmd = ["/opt/calicoctl/calicoctl"] + list(test_args)
    expected_env = os.environ.copy()
    expected_env.update({"ETCD_KEY_FILE": "/tmp/test/path/key"})

    charm.calicoctl(*test_args)

    mock_check.assert_called_once_with(
        expected_cmd, env=expected_env, stderr=subprocess.PIPE, timeout=60
    )


@mock.patch("subprocess.check_output")
@mock.patch("charm.CalicoCharm._get_calicoctl_env")
def test_calicoctl_raises(
    mock_get: mock.MagicMock, mock_check: mock.MagicMock, charm: CalicoCharm
):
    test_args = ("get", "version")
    expected_cmd = ["/opt/calicoctl/calicoctl"] + list(test_args)
    mock_check.side_effect = CalledProcessError(1, expected_cmd, "some output", "some error")

    with pytest.raises(CalledProcessError):
        charm.calicoctl(*test_args)


@mock.patch("charm.CalicoCharm._set_status")
def test_on_update_status(mock_set: mock.MagicMock, charm: CalicoCharm):
    mock_event = mock.MagicMock()
    charm._on_update_status(mock_event)
    mock_set.assert_called_once()
