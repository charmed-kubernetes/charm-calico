"""This module provides the CalicoManifests class for managing Calico manifests."""

import datetime
import hashlib
import json
import logging
from base64 import b64encode
from typing import Dict, FrozenSet, Iterable, List, Optional

from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from lightkube.codecs import AnyResource
from lightkube.core.client import Client
from lightkube.models.core_v1 import Container, EnvVar
from lightkube.resources.apps_v1 import DaemonSet
from lightkube.resources.core_v1 import Event, Pod
from ops.manifests import ConfigRegistry, HashableResource, ManifestLabel, Manifests, Patch
from ops.manifests.manipulations import AnyCondition

log = logging.getLogger(__name__)
MANIFEST_LABEL = "k8s-app"


class PatchCDKOnCAChange(Patch):
    """Patch Deployments/Daemonsets to be apart of cdk-restart-on-ca-change.

    * adding the config hash as an annotation
    * adding a cdk restart label
    """

    def __call__(self, obj: AnyResource) -> None:
        """Modify the calico-kube-controllers Deployment and calico-node DaemonSet."""
        if obj.kind not in ["Deployment", "DaemonSet"]:
            return

        title = f"{obj.kind}/{obj.metadata.name.title().replace('-', ' ')}"
        log.info(f"Patching {title} cdk-restart-on-ca-changed label.")
        label = {"cdk-restart-on-ca-change": "true"}
        obj.metadata.labels = obj.metadata.labels or {}
        obj.metadata.labels.update(label)

        log.info(f"Adding hash to {title}.")
        obj.spec.template.metadata.annotations = {
            "juju.is/manifest-hash": self.manifests.config_hash
        }


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
                ipauto_env = EnvVar(
                    "IP_AUTODETECTION_METHOD", f"cidr={self.manifests.config.get('ipv4_cidr')}"
                )
                env.append(ipauto_env)


class PatchValuesKubeControllers(Patch):
    """A patch class for allowing migration from EnvVars to Secrets in Kube Controllers."""

    NAME = "calico-kube-controllers"

    def __call__(self, obj: AnyResource) -> None:
        """Modify the calico-kube-controllers Deployment's environment variables."""
        if not (obj.kind == "Deployment" and obj.metadata.name == self.NAME):
            return

        containers: List[Container] = obj.spec.template.spec.containers
        for container in containers:
            if container.name == self.NAME:
                env = container.env
                for e in env:
                    if e.name.startswith("ETCD"):
                        # blank the `value` with <space> field rather using `None`
                        e.value = ""


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
        data.update({"veth_mtu": str(mtu) if mtu else "0"})


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

    CONFIG_MAP = {
        "etcd-key": "client_key",
        "etcd-cert": "client_cert",
        "etcd-ca": "client_ca",
    }

    def __call__(self, obj) -> None:
        """Modify the Calico etcd Secret by updating its data."""
        if not (obj.kind == "Secret" and obj.metadata.name == "calico-etcd-secrets"):
            return

        values = {}
        for secret_key, manifest_key in self.CONFIG_MAP.items():
            val = self.manifests.config.get(manifest_key)
            enc = self._encode_base64(val)
            if enc:
                values[secret_key] = enc

        if not values:
            log.info("Etcd secrets unavailable to patch.")
            return

        log.info("Patching Calico etcd Secret.")
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
            PatchValuesKubeControllers(self),
            PatchIPAutodetectionMethod(self),
            PatchEtcdPaths(self),
            PatchCDKOnCAChange(self),
            ManifestLabel(self),
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

    def status(self) -> FrozenSet[HashableResource]:
        """Return all installed objects which have a `.status.conditions` attribute.

        Bonus: Log events for daemonsets with unready pods.
        """
        installed = self.installed_resources()
        for obj in installed:
            if obj.kind == "DaemonSet":
                ds: DaemonSet = obj.resource
                if ds.status.numberReady != ds.status.desiredNumberScheduled:
                    log_events(collect_events(self.client, obj.resource))
        return frozenset(_ for _ in installed if _.status_conditions)

    def is_ready(self, obj: HashableResource, cond: AnyCondition) -> Optional[bool]:
        """Determine if the resource is ready."""
        if not (ready := super().is_ready(obj, cond)):
            log_events(collect_events(self.client, obj.resource))
        return ready


def by_localtime(event: Event) -> datetime.datetime:
    """Return the last timestamp of the event if available in local time, otherwise approximate with now."""
    dt = event.lastTimestamp or datetime.datetime.now(datetime.timezone.utc)
    return dt.astimezone()


def log_events(events: Iterable[Event]) -> None:
    """Log the events."""
    for event in sorted(events, key=by_localtime):
        log.info(
            "Event %s/%s %s msg=%s",
            event.involvedObject.kind,
            event.involvedObject.name,
            event.lastTimestamp and event.lastTimestamp.astimezone() or "Date not recorded",
            event.message,
        )


def collect_events(client: Client, resource: AnyResource) -> List[Event]:
    """Collect events from the resource."""
    kind: str = resource.kind or type(resource).__name__
    meta = resource.metadata
    object_events = list(
        client.list(
            Event,
            namespace=meta.namespace,
            fields={
                "involvedObject.kind": kind,
                "involvedObject.name": meta.name,
            },
        )
    )
    if kind in ["Deployment", "DaemonSet"]:
        involved_pods = client.list(
            Pod, namespace=meta.namespace, labels={MANIFEST_LABEL: meta.name}
        )
        object_events += [event for pod in involved_pods for event in collect_events(client, pod)]
    return object_events
