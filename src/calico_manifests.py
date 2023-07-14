"""This module provides the CalicoManifests class for managing Calico manifests."""
import hashlib
import json
import logging
from base64 import b64encode
from typing import Dict, List

from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from lightkube.codecs import AnyResource
from lightkube.models.core_v1 import Container, EnvVar
from ops.manifests import ConfigRegistry, Manifests, Patch

log = logging.getLogger(__name__)


class PatchCDKOnCAChange(Patch):
    """A Patch class for setting a label in calico-kube-controllers."""

    def __call__(self, obj) -> None:
        """Add the cdk-restart-on-ca-changed label to calico-kube-controllers."""
        if not (obj.kind == "Deployment" and obj.metadata.name == "calico-kube-controllers"):
            return

        log.info("Patching Calico Kube Controllers cdk-restart-on-ca-changed label.")
        label = {"cdk-restart-on-ca-change": "true"}
        obj.metadata.labels = obj.metadata.labels or {}
        obj.metadata.labels.update(label)


class PatchEtcdPaths(Patch):
    """A Patch class for setting the Etcd Paths in Calico."""

    def __call__(self, obj) -> None:
        """Modify the calico-config etcd variables to adjust the certificate paths."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "calico-config"):
            return

        log.info("Patching Calico etcd paths.")

        data = obj.data
        if not data:
            log.warning("calico-config: Unable to patch etcd paths, data not found.")
            return
        data.update(
            {
                "etcd_ca": "/calico-secrets/etcd-ca",
                "etcd_cert": "/calico-secrets/etcd-cert",
                "etcd_key": "/calico-secrets/etcd-key",
            }
        )


class PatchIPAutodetectionMethod(Patch):
    """A Patch class for IP autodetection method in Calico Node."""

    def __call__(self, obj) -> None:
        """Modify the calico-node DaemonSet's environment variables to adjust the IP auto-detection method."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "calico-node"):
            return

        log.info("Patching calico-node IP autodetection method.")
        containers: List[Container] = obj.spec.template.spec.containers
        for container in containers:
            if container.name == "calico-node":
                env = container.env
                ipauto_env = EnvVar("IP_AUTODETECTION_METHOD", "skip-interface=lxd.*,fan.*")
                env.append(ipauto_env)


class PatchValuesKubeControllers(Patch):
    """A patch class for allowing migration from EnvVars to Secrets in Kube Controllers."""

    def __call__(self, obj: AnyResource) -> None:
        """Modify the calico-kube-controllers Deployment's environment variables."""
        if not (obj.kind == "Deployment" and obj.metadata.name == "calico-kube-controllers"):
            return

        containers: List[Container] = obj.spec.template.spec.containers

        for container in containers:
            if container.name == "calico-kube-controllers":
                env = container.env
                for e in env:
                    if e.name.startswith("ETCD"):
                        # blank the `value` with <space> field rather using `None`
                        e.value = ""


class SetAnnotationCalicoNode(Patch):
    """A patch class for setting the annotation in a Node DaemonSet."""

    def __call__(self, obj) -> None:
        """Add the Config hash to the DaemonSet to force a restart."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "calico-node"):
            return

        log.info("Adding hash to calico-node DaemonSet.")

        obj.spec.template.metadata.annotations = {
            "juju.is/manifest-hash": self.manifests.config_hash
        }


class SetAnnotationKubeControllers(Patch):
    """A patch class for setting the annotation in a Kube Controllers Deployment."""

    def __call__(self, obj) -> None:
        """Add the Config hash to the Kube Controllers Deployment."""
        if not (obj.kind == "Deployment" and obj.metadata.name == "calico-kube-controllers"):
            return

        log.info("Adding hash to calico-node DaemonSet.")

        obj.spec.template.metadata.annotations = {
            "juju.is/manifest-hash": self.manifests.config_hash
        }


class PatchVethMtu(Patch):
    """A patch class for modifying the MTU value in a ConfigMap."""

    def __call__(self, obj) -> None:
        """Modify the Calico MTU within the given ConfigMap object."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "calico-config"):
            return

        log.info("Patching Calico MTU value.")

        data = obj.data
        if not data:
            log.warning("calico-config: Unable to patch MTU value, data not found.")
            return
        mtu = self.manifests.config.get("mtu")
        data.update({"veth_mtu": mtu if mtu else "0"})


class PatchCalicoConflist(Patch):
    """A patch class for modifying the Calico CNI Conflist in a ConfigMap."""

    def __call__(self, obj) -> None:
        """Modify the Calico Conflist within the given ConfigMap object."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "calico-config"):
            return

        log.info("Patching Calico Conflist.")
        data = obj.data
        if not data:
            log.warning("calico-config: Unable to patch conflist, data not found.")
            return

        json_config = data.get("cni_network_config")

        if not json_config:
            return

        # Replace mtu value to avoid json errors
        json_config = json_config.replace("__CNI_MTU__", '"__CNI_MTU__"')

        conflist = json.loads(json_config)

        for plugin in conflist.get("plugins"):
            if plugin.get("type") == "calico":
                ipam = plugin["ipam"]
                ipam["assign_ipv4"] = self.manifests.config.get("assign_ipv4")
                ipam["assign_ipv6"] = self.manifests.config.get("assign_ipv6")

        json_config = json.dumps(conflist)
        json_config = json_config.replace('"__CNI_MTU__"', "__CNI_MTU__")

        data["cni_network_config"] = json_config


class SetIPv6Configuration(Patch):
    """A Patch class for setting the IPv6 configuration for Calico."""

    def __call__(self, obj) -> None:
        """Set the IPv6 configuration within the given calico/node container."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "calico-node"):
            return

        log.info("Patching calico-node DaemonSet IPv6.")

        containers: List[Container] = obj.spec.template.spec.containers
        enable = self.manifests.config.get("IP6") == "autodetect"
        vars = {
            "IP6": {"value": self.manifests.config.get("IP6"), "found": False},
            "FELIX_IPV6SUPPORT": {"value": "true" if enable else "false", "found": False},
        }

        for container in containers:
            if container.name == "calico-node":
                env = container.env

                for v in vars:
                    for e in env:
                        if e.name == v:
                            vars[v]["found"] = True
                            e.value = vars[v]["value"]
                for v in vars:
                    if not vars[v]["found"]:
                        env.append(EnvVar(v, vars[v]["value"]))


class SetNoDefaultPools(Patch):
    """A Patch class for setting the NO_DEFAULT_POOLS environmental variable."""

    def __call__(self, obj) -> None:
        """Set the NO_DEFAULT_POOLS within the given calico/node container."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "calico-node"):
            return

        log.info("Patching calico-node DaemonSet NO_DEFAULT_POOLS.")

        containers: List[Container] = obj.spec.template.spec.containers

        for container in containers:
            if container.name == "calico-node":
                env = container.env
                value = "true" if self.manifests.config.get("manage-pools") else "false"
                for e in env:
                    if e.name == "NO_DEFAULT_POOLS":
                        e.value = value
                        return
                ignore_env = EnvVar("NO_DEFAULT_POOLS", value)
                env.append(ignore_env)


class SetIgnoreLooseRPF(Patch):
    """A Patch class for setting the FELIX_IGNORELOOSERPF environmental variable."""

    def __call__(self, obj) -> None:
        """Set the FELIX_IGNORELOOSERPF within the given calico/node container."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "calico-node"):
            return
        log.info("Patching calico-node DaemonSet.")
        containers: List[Container] = obj.spec.template.spec.containers
        for container in containers:
            if container.name == "calico-node":
                env = container.env
                value = "true" if self.manifests.config.get("ignore-loose-rpf") else "false"
                for e in env:
                    if e.name == "FELIX_IGNORELOOSERPF":
                        e.value = value
                        return
                ignore_env = EnvVar("FELIX_IGNORELOOSERPF", value)
                env.append(ignore_env)


class SetEtcdEndpoints(Patch):
    """A Patch class for setting the etcd endpoints in a Calico ConfigMap."""

    def __call__(self, obj) -> None:
        """Set the etcd endpoints within the given ConfigMap object."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "calico-config"):
            return

        log.info("Patching Calico etcd connection string.")

        uri = self.manifests.etcd.get_connection_string()
        data = obj.data
        if not data:
            log.warning("calico-config: Unable to patch etcd endpoints, data not found.")
            return
        data.update({"etcd_endpoints": uri})


class SetEtcdSecrets(Patch):
    """A Patch class for modifying the Calico etcd Secret."""

    def __call__(self, obj) -> None:
        """Modify the Calico etcd Secret by updating its data."""
        if not (obj.kind == "Secret" and obj.metadata.name == "calico-etcd-secrets"):
            return

        log.info("Patching Calico etcd Secret.")
        values = {
            "etcd-key": self._encode_base64(self.manifests.config["client_key"]),
            "etcd-cert": self._encode_base64(self.manifests.config["client_cert"]),
            "etcd-ca": self._encode_base64(self.manifests.config["client_ca"]),
        }
        data = obj.data
        if data:
            data.update(values)
        else:
            obj.data = values

    def _encode_base64(self, data: str) -> str:
        """Encode data in Base64 format.

        Args:
            data (str): The data to encode.

        Returns:
            str: The encoded data in Base64 format.
        """
        if not data:
            return ""
        return b64encode(data.encode()).decode()


class CalicoManifests(Manifests):
    """A class representing Calico manifests.

    This class extends the Manifests class and provides functionality specific to Calico manifests.

    Args:
        charm (CharmBase): The charm object.
        charm_config (dict): The charm configuration.
        etcd (EtcdReactiveRequires): The Etcd relation object.
        cni_config (dict): The CNI (Container Network Interface) configuration.

    Attributes:
        charm_config (dict): The charm configuration.
        etcd (EtcdReactiveRequires): The Etcd relation object.
        cni_config (dict): The CNI (Container Network Interface) configuration.

    Methods:
        config: Returns the configuration mapped from the charm config and joined relations.
    """

    def __init__(self, charm, charm_config, etcd: EtcdReactiveRequires, cni_config: dict):
        """Initialize an instance of CalicoManifests.

        Args:
            charm (CharmBase): The Calico charm object.
            charm_config (dict): The charm configuration.
            etcd (EtcdReactiveRequires): The Etcd relation object.
            cni_config (dict): The CNI (Container Network Interface) configuration.
        """
        manipulations = [
            ConfigRegistry(self),
            SetEtcdEndpoints(self),
            SetEtcdSecrets(self),
            PatchCalicoConflist(self),
            SetIgnoreLooseRPF(self),
            SetIPv6Configuration(self),
            PatchVethMtu(self),
            SetAnnotationCalicoNode(self),
            SetAnnotationKubeControllers(self),
            PatchValuesKubeControllers(self),
            PatchIPAutodetectionMethod(self),
            PatchEtcdPaths(self),
            PatchCDKOnCAChange(self),
        ]

        super().__init__("calico", charm.model, "upstream/calico", manipulations)
        self.charm_config = charm_config
        self.etcd = etcd
        self.cni_config = cni_config

    @property
    def config(self) -> Dict:
        """Return the configuration mapped from the charm config and joined relations.

        Returns:
            dict: The merged configuration.
        """
        config = {}
        config.update({"connection_string": self.etcd.get_connection_string()})
        config.update(self.etcd.get_client_credentials())

        config.update(**self.charm_config)
        config.update(**self.cni_config)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("release", None)
        return config

    @property
    def config_hash(self) -> str:
        """Return the configuration SHA256 hash from the charm config.

        Returns:
            str: The SHA256 hash
        """
        json_str = json.dumps(self.config, sort_keys=True)
        hash = hashlib.sha256()
        hash.update(json_str.encode())
        return hash.hexdigest()
