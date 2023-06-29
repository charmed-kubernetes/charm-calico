"""This module provides the CalicoManifests class for managing Calico manifests."""
import json
import logging
from base64 import b64encode
from typing import Dict

from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from ops.manifests import ConfigRegistry, Manifests, Patch

log = logging.getLogger(__name__)


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

        conflist = json.loads(json_config)

        for plugin in conflist.get("plugins"):
            if plugin.get("type") == "calico":
                mtu = self.manifests.config.get("mtu")
                plugin["mtu"] = mtu if mtu else 0
                ipam = plugin["ipam"]
                ipam["assign_ipv4"] = self.manifests.config.get("assign_ipv4")
                ipam["assign_ipv6"] = self.manifests.config.get("assign_ipv6")

        data["cni_network_config"] = json.dumps(conflist)


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
    # TODO: Hash and put into the annotations to reload both node and controller
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
        config.update(self.etcd.get_client_credentials())

        config.update(**self.charm_config)
        config.update(**self.cni_config)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("release", None)
        return config
