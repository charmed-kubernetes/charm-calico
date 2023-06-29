# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing


import os
from subprocess import CalledProcessError
from typing import Optional
import unittest.mock as mock

import ops
from ops.manifests import ManifestClientError
from ops.model import ActiveStatus, BlockedStatus
import ops.testing
import pytest
from yaml import BlockEntryToken, YAMLError
from charm import CalicoCharm
from ops.testing import Harness

ops.testing.SIMULATE_CAN_CONNECT = True


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
        pytest.param(False, id="Reactive bits not found"),
    ],
)
@mock.patch("charm.CalicoCharm.service_running")
@mock.patch("charm.CalicoCharm.service_stop")
@mock.patch("charm.os.path.isfile")
@mock.patch("charm.os.remove")
def test_remove_calico_reactive(
    mock_running: mock.MagicMock,
    mock_stop: mock.MagicMock,
    mock_isfile: mock.MagicMock,
    mock_remove: mock.MagicMock,
    charm: CalicoCharm,
    detected: bool,
):
    service_path = os.path.join(os.sep, "lib", "systemd", "system", "calico-node.service")
    mock_running.return_value = detected
    mock_stop.return_value = detected
    mock_isfile.return_value = detected

    charm._remove_calico_reactive()
    mock_running.assert_called_once_with("calico-node")
    mock_stop.assert_called_once_with("calico-node")
