#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.

"""Charm the service."""

import ipaddress
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from socket import gethostname
from subprocess import CalledProcessError, TimeoutExpired
from typing import Set

import ops
import yaml
from calico_manifests import CalicoManifests
from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from charms.operator_libs_linux.v1.systemd import daemon_reload, service_running, service_stop
from ops.framework import StoredState
from ops.manifests import Collector, ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, ModelError, WaitingStatus

log = logging.getLogger(__name__)

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]

CALICO_CTL_PATH = "/opt/calicoctl"
CALICO_CNI_CONFIG_PATH = "/etc/cni/net.d/10-calico.conflist"
ETCD_KEY_PATH = os.path.join(CALICO_CTL_PATH, "etcd-key")
ETCD_CERT_PATH = os.path.join(CALICO_CTL_PATH, "etcd-cert")
ETCD_CA_PATH = os.path.join(CALICO_CTL_PATH, "etcd-ca")


class CalicoCharm(ops.CharmBase):
    """Charm the service."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.etcd = EtcdReactiveRequires(self)
        self.cni_options = {}
        self.stored.set_default(
            binaries_installed=False,
            calico_configured=False,
            service_installed=False,
            credentials_available=False,
            cni_configured=False,
            ncp_deployed=False,
            deployed=False,
        )
        self.calico_manifests = CalicoManifests(self, self.config, self.etcd, self.cni_options)
        self.collector = Collector(self.calico_manifests)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)

        self.framework.observe(self.etcd.on.connected, self._on_etcd_connected)
        self.framework.observe(self.etcd.on.available, self._update_calicoctl_env)
        self.framework.observe(self.etcd.on.tls_available, self._on_etcd_changed)

        self.framework.observe(self.on.list_versions_action, self._list_versions)
        self.framework.observe(self.on.list_resources_action, self._list_resources)
        self.framework.observe(self.on.scrub_resources_action, self._scrub_resources)
        self.framework.observe(self.on.sync_resources_action, self._sync_resources)

    def _on_etcd_changed(self, event):
        self.etcd.save_client_credentials(ETCD_CA_PATH, ETCD_CERT_PATH, ETCD_KEY_PATH)
        self.unit.status = MaintenanceStatus("Updating etcd configuration.")
        if self.stored.deployed:
            try:
                self.calico_manifests.apply_manifests()
            except ManifestClientError:
                log.exception("Failed to update etcd secrets.")
                event.defer()

    def _on_config_changed(self, event):
        self.unit.status = MaintenanceStatus("Reconfiguring Calico.")
        if self.stored.deployed:
            try:
                self._configure_calico()
                self.calico_manifests.apply_manifests()
                self._set_status()
            except ManifestClientError:
                self.unit.status = WaitingStatus("Waiting for Kubernetes API.")
                log.exception("Failed to apply manifests, will retry.")
                event.defer()
                return
            except (CalledProcessError, TimeoutExpired):
                self.unit.status = WaitingStatus("Configuring Calico.")
                log.exception("Failed to configure Calico, will retry.")
                event.defer()
                return
            except yaml.YAMLError:
                log.exception("Invalid configuration provided:")
                self.unit.status = BlockedStatus(
                    "Invalid Config provided. Please check juju debug-log for more info."
                )
                return

    def _configure_calico(self):
        try:
            self._configure_calico_pool()
            self._configure_node()
            self._configure_bgp_globals()
            self._configure_bgp_peers()
            self._disable_vxlan_tx_checksumming()
            self.stored.calico_configured = True
        except yaml.YAMLError as e:
            self.stored.calico_configured = False
            raise e

    def _on_upgrade(self, event):
        self._remove_calico_reactive()
        self.stored.binaries_installed = False
        self.stored.deployed = False
        self._install_or_upgrade(event)

    def _on_install(self, event):
        self._install_or_upgrade(event)

    def _get_kubeconfig_status(self):
        for relation in self.model.relations["cni"]:
            for unit in relation.units:
                if relation.data[unit].get("kubeconfig-hash"):
                    return True
        return False

    def _install_or_upgrade(self, event):
        # Handle installation if not done yet.
        if not self.stored.binaries_installed:
            self._install_calico_binaries()

        # Ensure kubeconfig status before moving ahead.
        if not self._get_kubeconfig_status():
            self.unit.status = WaitingStatus("Waiting for Kubernetes config.")
            log.info("Kubeconfig unavailable, will retry.")
            event.defer()
            return

        if not self.etcd.is_ready:
            self.unit.status = BlockedStatus("Waiting for etcd.")
            log.info("etcd is not ready, will retry.")
            event.defer()
            return

        # If credentials are available, install manifests and configure cni if not already done.
        if not self.stored.deployed:
            try:
                self.etcd.save_client_credentials(ETCD_CA_PATH, ETCD_CERT_PATH, ETCD_KEY_PATH)
                self._configure_cni()
                self.calico_manifests.apply_manifests()
                self._configure_calico()
                self.stored.deployed = True
                self._set_status()
            except ManifestClientError:
                self.unit.status = WaitingStatus("Installing Calico manifests")
                log.exception("Failed to install Calico manifests, will retry.")
                event.defer()
                return
            except (CalledProcessError, TimeoutExpired):
                self.unit.status = WaitingStatus("Configuring Calico")
                log.exception("Failed to configure Calico, will retry.")
                event.defer()
                return
            except yaml.YAMLError:
                log.exception("Failed to configure Calico")
                self.unit.status = BlockedStatus(
                    "Invalid Config provided. Please check juju debug-log for more info."
                )
                return

    def _set_status(self):
        if self._is_rpf_config_mismatched():
            self.unit.status = BlockedStatus(
                "ignore-loose-rpf config is in conflict with rp_filter value"
            )
            return
        if self.stored.deployed and self.stored.calico_configured:
            self.unit.set_workload_version(self.collector.short_version)
            self.unit.status = ActiveStatus("Ready")

    def _on_etcd_connected(self, _):
        self.unit.status = BlockedStatus("Waiting for relation to etcd.")

    def _get_mtu(self) -> int:
        """Get the user-specified MTU size, adjusted to make room for encapsulation headers.

        This method retrieves the MTU size specified by the user in the charm configuration and adjusts
        it to make room for encapsulation headers. The adjustment is based on the tunneling protocol used
        by Calico, which can add additional headers to packets.
        https://docs.projectcalico.org/networking/mtu

        Returns:
            int: The adjusted MTU size, or None if the MTU size is not specified in the charm configuration.
        """
        mtu = self.config.get("veth-mtu")
        if not mtu:
            return None
        if self.config["vxlan"] != "Never":
            return mtu - 50
        if self.config["ipip"] != "Never":
            return mtu - 20
        return mtu

    def _remove_calico_reactive(self):
        self.unit.status = MaintenanceStatus("Removing Reactive resources.")

        service_path = os.path.join(os.sep, "lib", "systemd", "system", "calico-node.service")
        service_name = "calico-node"
        if service_running(service_name):
            if service_stop(service_name):
                log.info(f"{service_name} service stopped.")
        else:
            log.info(f"{service_name} service successfully stopped.")

        if os.path.isfile(service_path):
            os.remove(service_path)
            daemon_reload()
            log.info(f"{service_name} service removed and daemon reloaded.")
        else:
            log.info(f"{service_name} service successfully uninstalled.")

    def _get_ip_versions(self) -> Set[int]:
        return {net.version for net in self._get_networks(self.config["cidr"])}

    def _configure_bgp_globals(self):
        self.unit.status = MaintenanceStatus("Configuring BGP globals.")

        try:
            bgp_config = self._calicoctl_get("bgpconfig", "default")
        except (CalledProcessError, TimeoutExpired) as e:
            if b"resource does not exist" in e.stderr:
                log.warning("default BGPConfiguration does not exist.")
                bgp_config = {
                    "apiVersion": "projectcalico.org/v3",
                    "kind": "BGPConfiguration",
                    "metadata": {"name": "default"},
                    "spec": {},
                }
            else:
                log.exception("Failed to get BGPConfiguration")
                raise e

        ip_mapping = {
            "bgp-service-cluster-ips": "serviceClusterIPs",
            "bgp-service-external-ips": "serviceExternalIPs",
            "bgp-service-loadbalancer-ips": "serviceLoadBalancerIPs",
        }
        spec = bgp_config["spec"]
        spec["asNumber"] = self.config["global-as-number"]
        spec["nodeToNodeMeshEnabled"] = self.config["node-to-node-mesh"]
        spec.update(
            {
                ip_mapping[key]: [{"cidr": cidr} for cidr in self.config[key].split()]
                for key in ip_mapping
            }
        )
        try:
            self._calicoctl_apply(bgp_config)
            log.info("Configured BGP globals.")
        except (CalledProcessError, TimeoutExpired) as e:
            log.exception("Failed to apply BGPConfiguration")
            raise e

    def _configure_node(self):
        self.unit.status = MaintenanceStatus("Configuring Calico node.")

        node_name = gethostname()
        as_number = self._get_unit_as_number()
        route_reflector_cluster_id = self._get_route_reflector_cluster_id()

        try:
            node = self._calicoctl_get("node", node_name)
            node["spec"]["bgp"]["asNumber"] = as_number
            node["spec"]["bgp"]["routeReflectorClusterID"] = route_reflector_cluster_id
            self._calicoctl_apply(node)
            log.info("Configured Calico node.")
        except (CalledProcessError, TimeoutExpired) as e:
            log.exception("Failed to configure node.")
            raise e

    def _get_route_reflector_cluster_id(self):
        route_reflector_cluster_ids = yaml.safe_load(self.config["route-reflector-cluster-ids"])
        unit_id = self._get_unit_id()
        return route_reflector_cluster_ids.get(unit_id)

    def _get_unit_as_number(self):
        unit_id = self._get_unit_id()
        unit_as_numbers = yaml.safe_load(self.config["unit-as-numbers"])
        if unit_id in unit_as_numbers:
            return unit_as_numbers[unit_id]

        subnet_as_numbers = yaml.safe_load(self.config["subnet-as-numbers"])
        subnets = self._filter_local_subnets(subnet_as_numbers)
        if subnets:
            subnets.sort(key=lambda subnet: -subnet.prefixlen)
            subnet = subnets[0]
            as_number = subnet_as_numbers.get(str(subnet))
            return as_number

        return None

    def _filter_local_subnets(self, subnets):
        bind_address = ipaddress.ip_address(self._get_bind_address())
        filtered_subnets = [
            ipaddress.ip_network(subnet)
            for subnet in subnets
            if bind_address in ipaddress.ip_network(subnet)
        ]
        return filtered_subnets

    def _get_bind_address(self):
        bind_address = self.model.get_binding("cni").network.bind_address
        return bind_address

    def _configure_bgp_peers(self):
        self.unit.status = MaintenanceStatus("Configuring BGP peers.")

        peers = []
        peers += yaml.safe_load(self.config["global-bgp-peers"])

        subnet_bgp_peers = yaml.safe_load(self.config["subnet-bgp-peers"])
        subnets = self._filter_local_subnets(subnet_bgp_peers)
        for subnet in subnets:
            peers += subnet_bgp_peers.get(str(subnet), [])

        unit_id = self._get_unit_id()
        unit_bgp_peers = yaml.safe_load(self.config["unit-bgp-peers"])
        if unit_id in unit_bgp_peers:
            peers += unit_bgp_peers[unit_id]

        safe_unit_name = self.unit.name.replace("/", "-")
        named_peers = {
            f"{safe_unit_name}-{peer['address'].replace(':', '-')}-{peer['as-number']}": peer
            for peer in peers
        }

        try:
            node_name = gethostname()
            for peer_name, peer in named_peers.items():
                peer_def = {
                    "apiVersion": "projectcalico.org/v3",
                    "kind": "BGPPeer",
                    "metadata": {
                        "name": peer_name,
                    },
                    "spec": {
                        "node": node_name,
                        "peerIP": peer["address"],
                        "asNumber": peer["as-number"],
                    },
                }
                self._calicoctl_apply(peer_def)

            log.info("Removing unrecognized peers.")
            existing_peers = self._calicoctl_get("bgppeers")["items"]
            existing_peers = [peer["metadata"]["name"] for peer in existing_peers]
            peers_to_delete = [
                peer
                for peer in existing_peers
                if peer.startswith(safe_unit_name + "-") and peer not in named_peers
            ]

            for peer in peers_to_delete:
                self.calicoctl("delete", "bgppeer", peer)
            log.info("Configured BGP peers.")
        except (CalledProcessError, TimeoutExpired) as e:
            log.exception("Failed to apply BGP peer configuration.")
            raise e

    def _get_unit_id(self):
        return int(self.unit.name.split("/")[1])

    def _configure_cni(self):
        """Configure calico cni."""
        self.unit.status = MaintenanceStatus("Configuring Calico CNI.")
        ip_versions = self._get_ip_versions()
        ip6 = "autodetect" if 6 in ip_versions else "none"
        self.cni_options.update(
            {
                "kubeconfig_path": "/opt/calicoctl/kubeconfig",
                "mtu": self._get_mtu(),
                "assign_ipv4": "true" if 4 in ip_versions else "false",
                "assign_ipv6": "true" if 6 in ip_versions else "false",
                "IP6": ip6,
            }
        )
        self._propagate_cni_config()
        self.stored.cni_configured = True

    def _disable_vxlan_tx_checksumming(self):
        if self.config["disable-vxlan-tx-checksumming"] and self.config["vxlan"] != "Never":
            cmd = ["ethtool", "-K", "vxlan.calico", "tx-checksum-ip-generic", "off"]
            try:
                subprocess.check_call(cmd)
                log.info("Disabled VXLAN TX checksumming.")
            except CalledProcessError as e:
                log.exception("Couldn't disable tx checksumming.")
                raise e

    def _propagate_cni_config(self):
        self.unit.status = MaintenanceStatus("Propagating CNI config.")
        cidr = self.config["cidr"]
        for r in self.model.relations["cni"]:
            r.data[self.unit]["cidr"] = cidr
            r.data[self.unit]["cni-conf-file"] = "10-calico.conflist"

    def _configure_calico_pool(self):
        if not self.config["manage-pools"]:
            log.info("Skipping pool configuration.")
            return

        self.unit.status = MaintenanceStatus("Configuring Calico IP pool.")

        try:
            pools = self._calicoctl_get("pool")["items"]

            cidrs = tuple(cidr.strip() for cidr in self.config["cidr"].split(","))
            names = tuple(f"ipv{self._get_network(cidr).version}" for cidr in cidrs)
            pool_names_to_delete = [
                pool["metadata"]["name"]
                for pool in pools
                if pool["metadata"]["name"] not in names or pool["spec"]["cidr"] not in cidrs
            ]

            for pool_name in pool_names_to_delete:
                log.info(f"Deleting Pool: {pool_name}")
                self.calicoctl("delete", "pool", pool_name, "--skip-not-exists")

            for cidr, name in zip(cidrs, names):
                pool = {
                    "apiVersion": "projectcalico.org/v3",
                    "kind": "IPPool",
                    "metadata": {"name": name},
                    "spec": {
                        "cidr": cidr,
                        "ipipMode": self.config["ipip"],
                        "vxlanMode": self.config["vxlan"],
                        "natOutgoing": self.config["nat-outgoing"],
                    },
                }

                self._calicoctl_apply(pool)
            log.info("Configured Calico IP pool.")

        except (CalledProcessError, TimeoutExpired) as e:
            log.exception("Failed to modify IP Pools.")
            raise e

    def _on_update_status(self, _):
        self._set_status()

    def _install_calico_binaries(self):
        arch = self._get_arch()
        resource_name = "calico" if arch == "amd64" else f"calico-{arch}"

        try:
            resource_path = self.model.resources.fetch("calico")
        except ModelError:
            self.unit.status = BlockedStatus(f"Error claiming {resource_name}")
            log.exception(f"Error claiming {resource_name}")
            return
        except NameError:
            self.unit.status = BlockedStatus(f"Resource {resource_name} not found")
            log.exception(f"Resource {resource_name} not found")
            return

        filesize = os.stat(resource_path).st_size
        if filesize < 1000000:
            self.unit.status = BlockedStatus(f"Incomplete resource: {resource_name}")
            return

        self.unit.status = MaintenanceStatus("Unpacking Calico resource.")

        with tempfile.TemporaryDirectory() as tmp:
            self._unpack_archive(resource_path, tmp)
            origin = os.path.join(tmp, "calicoctl")
            dst = os.path.join(CALICO_CTL_PATH, "calicoctl")
            install_cmd = ["install", "-v", "-D", origin, dst]
            try:
                subprocess.check_call(install_cmd)
            except CalledProcessError:
                msg = "Failed to install calicoctl"
                log.exception(msg)
                self.unit.status = BlockedStatus(msg)
                return

            calicoctl_path = "/usr/local/bin/calicoctl"
            shutil.copy(Path("./scripts/calicoctl"), calicoctl_path)
            os.chmod(calicoctl_path, 0o755)

        self.stored.binaries_installed = True

    def _update_calicoctl_env(self, _):
        env = self._get_calicoctl_env()
        lines = [f"export {key}={value}" for key, value in sorted(env.items())]
        output = "\n".join(lines)
        calicoctl_env = Path("/opt/calicoctl/calicoctl.env")
        calicoctl_env.parent.mkdir(parents=True, exist_ok=True)
        with calicoctl_env.open("w") as f:
            f.write(output)

    def _get_calicoctl_env(self):
        env = {}
        env["ETCD_ENDPOINTS"] = self.etcd.get_connection_string()
        env["ETCD_KEY_FILE"] = ETCD_KEY_PATH
        env["ETCD_CERT_FILE"] = ETCD_CERT_PATH
        env["ETCD_CA_CERT_FILE"] = ETCD_CA_PATH
        return env

    def _unpack_archive(self, path: Path, destination: Path):
        tar = tarfile.open(path)
        tar.extractall(destination)
        tar.close()

    def _list_versions(self, event):
        self.collector.list_versions(event)

    def _list_resources(self, event):
        resources = event.params.get("resources", "")
        return self.collector.list_resources(event, manifests=None, resources=resources)

    def _scrub_resources(self, event):
        resources = event.params.get("resources", "")
        return self.collector.scrub_resources(event, manifests=None, resources=resources)

    def _sync_resources(self, event):
        resources = event.params.get("resources", "")
        try:
            self.collector.apply_missing_resources(event, manifests=None, resources=resources)
        except ManifestClientError:
            msg = "Failed to apply missing resources. API Server unavailable."
            event.set_results({"result": msg})
        else:
            self.stored.deployed = True

    def _get_arch(self) -> str:
        """Retrieve the machine architecture as a string.

        This method uses the `dpkg` command to retrieve the machine architecture
        of the current system. The architecture is returned as a string.

        Returns:
            str: The machine architecture as a string.
        """
        architecture = subprocess.check_output(["dpkg", "--print-architecture"]).rstrip()
        architecture = architecture.decode("utf-8")
        return architecture

    def _get_network(self, cidr: str):
        """Retrieve the network address from a given CIDR.

        Args:
            cidr (str): The CIDR (Classless Inter-Domain Routing) notation specifying the IP address range.

        Returns:
            IPv4Network|IPv6Network: The network address derived from the CIDR.

        Example:
            get_network('192.168.0.0/24') returns IPv4Network('192.168.0.0/24')
        """
        return ipaddress.ip_interface(address=cidr).network

    def _get_networks(self, cidrs: str):
        """Retrieve a list of network addresses from a comma-separated string of CIDRs.

        Args:
            cidrs (str): A comma-separated string of CIDRs (Classless Inter-Domain Routing) specifying IP address ranges.

        Returns:
            List[IPv4Network|IPv6Network]: A list of network addresses derived from the given CIDRs.

        Example:
            get_networks('192.168.0.0/24,10.0.0.0/16') returns [IPv4Network('192.168.0.0/24'), IPv4Network('10.0.0.0/16')]
        """
        return [self._get_network(cidr) for cidr in cidrs.split(",")]

    def _is_rpf_config_mismatched(self):
        with open("/proc/sys/net/ipv4/conf/all/rp_filter") as f:
            rp_filter = int(f.read())
        ignore_loose_rpf = self.config.get("ignore-loose-rpf")
        if rp_filter == 2 and not ignore_loose_rpf:
            # calico says this is invalid
            # https://github.com/kubernetes-sigs/kind/issues/891
            return True
        return False

    def calicoctl(self, *args, timeout: int = 60):
        """Call calicoctl with specified args.

        @param int timeout: If the process does not terminate after timeout seconds,
                            raise a TimeoutExpired exception
        """
        cmd = ["/opt/calicoctl/calicoctl"] + list(args)
        env = os.environ.copy()
        env.update(self._get_calicoctl_env())
        try:
            return subprocess.check_output(cmd, env=env, stderr=subprocess.PIPE, timeout=timeout)
        except (CalledProcessError, TimeoutExpired) as e:
            log.error(e.stderr)
            log.error(e.output)
            raise

    def _calicoctl_get(self, *args):
        args = ["get", "-o", "yaml", "--export"] + list(args)
        output = self.calicoctl(*args)
        try:
            result = yaml.safe_load(output)
        except yaml.YAMLError:
            log.exception(f"Failed to parse calicoctl output as yaml:\n {output}")
            raise
        return result

    def _calicoctl_apply(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            filename = os.path.join(tmp, "calicoctl_manifest.yaml")
            with open(filename, "w") as f:
                yaml.dump(data, f)
            self.calicoctl("apply", "-f", filename)


if __name__ == "__main__":  # pragma: nocover
    ops.main(CalicoCharm)
