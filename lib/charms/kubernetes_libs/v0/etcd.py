"""TODO: Add a proper docstring here.

This is a placeholder docstring for this charm library. Docstrings are
presented on Charmhub and updated whenever you push a new version of the
library.

Complete documentation about creating and documenting libraries can be found
in the SDK docs at https://juju.is/docs/sdk/libraries.

See `charmcraft publish-lib` and `charmcraft fetch-lib` for details of how to
share and consume charm libraries. They serve to enhance collaboration
between charmers. Use a charmer's libraries for classes that handle
integration with their charm.

Bear in mind that new revisions of the different major API versions (v0, v1,
v2 etc) are maintained independently.  You can continue to update v0 and v1
after you have pushed v3.

Markdown is supported, following the CommonMark specification.
"""

import hashlib
import json
import logging
import os
from functools import cached_property

from ops.framework import EventBase, EventSource, Object, ObjectEvents, StoredState
from ops.model import Relation

# The unique Charmhub library identifier, never change it
LIBID = "6ff313b3031a4ab0a1b034ca3ba9d901"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

log = logging.getLogger(__name__)


class EtcdAvailable(EventBase):
    """Event emitted when the etcd relation data is available."""

    pass


class EtcdConnected(EventBase):
    """Event emitted when the etcd relation is connected."""

    pass


class EtcdTLSAvailable(EventBase):
    """Event emitted when the etcd relation TLS data is available."""

    pass


class EtcdConsumerEvents(ObjectEvents):
    """Events emitted by the etcd translation interface."""

    available = EventSource(EtcdAvailable)
    connected = EventSource(EtcdConnected)
    tls_available = EventSource(EtcdTLSAvailable)


class EtcdReactiveRequires(Object):
    """Requires side of the etcd interface.

    This class is a translation interface that wraps the requires side
    of the reactive etcd interface.
    """

    state = StoredState()
    on = EtcdConsumerEvents()

    def __init__(self, charm, endpoint="etcd"):
        super().__init__(charm, f"relation-{endpoint}")
        self.charm = charm
        self.endpoint = endpoint

        self.state.set_default(
            connected=False, available=False, tls_available=False, connection_string=""
        )

        for event in (
            charm.on[endpoint].relation_created,
            charm.on[endpoint].relation_joined,
            charm.on[endpoint].relation_changed,
            charm.on[endpoint].relation_departed,
            charm.on[endpoint].relation_broken,
        ):
            self.framework.observe(event, self._check_relation)

    def _check_relation(self, _: EventBase):
        """Check if the relation is available and emit the appropriate event."""
        # TODO: Fix in case the values changed, so emit an event to reconfigure.
        # etcd is connected only if the charm joins or change the relation
        if self.relation:
            self.state.connected = True
            self.on.connected.emit()
            # etcd is available only if the connection string is available
            if self.get_connection_string():
                self.state.available = True
                self.on.available.emit()
                # etcd tls is available only if the tls data is available
                # (i.e. client cert, client key, ca cert)
                cert = self.get_client_credentials()
                if cert["client_cert"] and cert["client_key"] and cert["client_ca"]:
                    self.state.tls_available = True
                    self.on.tls_available.emit()

    def _get_dict_hash(self, data: dict) -> str:
        """Generate a SHA-256 hash for a dictionary.

        This function converts the dictionary into a JSON string, ensuring it
        is sorted in order. It then generates a SHA-256 hash of this string.

        Args:
            data(dict): The dictionary to be hashed.

        Returns:
            str: The hexadecimal representation of the hash of the dictionary.
        """
        dump = json.dumps(data, sort_keys=True)
        hash_obj = hashlib.sha256()
        hash_obj.update(dump.encode())
        return hash_obj.hexdigest()

    @property
    def is_ready(self):
        """Check if the relation is available and emit the appropriate event."""
        if self.relation:
            if self.get_connection_string():
                cert = self.get_client_credentials()
                if all(cert.get(key) for key in ["client_cert", "client_key", "client_ca"]):
                    return True
        return False

    def get_connection_string(self) -> str:
        """Return the connection string for etcd."""
        remote_data = self._remote_data
        if remote_data:
            return remote_data.get("connection_string")
        return ""

    def get_client_credentials(self) -> dict:
        """Return the client credentials for etcd."""
        remote_data = self._remote_data
        return {
            "client_cert": remote_data.get("client_cert"),
            "client_key": remote_data.get("client_key"),
            "client_ca": remote_data.get("client_ca"),
        }

    @cached_property
    def relation(self) -> Relation | None:
        """Return the relation object for this interface."""
        return self.model.get_relation(self.endpoint)

    @property
    def _remote_data(self):
        """Return the remote relation data for this interface."""
        if not (self.relation and self.relation.units):
            return {}

        first_unit = next(iter(self.relation.units), None)
        data = self.relation.data[first_unit]
        return data

    def save_client_credentials(self, ca_path, cert_path, key_path):
        """Save all the client certificates for etcd to local files."""
        credentials = {"client_key": key_path, "client_cert": cert_path, "client_ca": ca_path}
        for key, path in credentials.items():
            self._save_remote_data(key, path)

    def _save_remote_data(self, key: str, path: str):
        """Save the remote data to a file."""
        value = self._remote_data.get(key)
        if value:
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "w") as stream:
                stream.write(value)
