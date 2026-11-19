import json
import unittest.mock as mock
from pathlib import Path

import pytest
from lightkube import codecs

from calico_manifests import PatchCalicoConflist

MANIFESTS = Path("upstream/calico/manifests")


def plugins_of(config_map):
    conflist = config_map.data["cni_network_config"].replace("__CNI_MTU__", '"__CNI_MTU__"')
    return json.loads(conflist)["plugins"]


@pytest.mark.parametrize("release", sorted(p.name for p in MANIFESTS.iterdir()))
def test_patch_calico_conflist_keeps_bandwidth(release):
    (path,) = (MANIFESTS / release).glob("*.yaml")
    (config_map,) = (
        obj
        for obj in codecs.load_all_yaml(path.read_text())
        if obj.kind == "ConfigMap" and obj.metadata.name == "calico-config"
    )
    original = plugins_of(config_map)
    manifests = mock.MagicMock(config={"assign_ipv4": "true", "assign_ipv6": "false"})

    PatchCalicoConflist(manifests)(config_map)

    patched = plugins_of(config_map)
    bandwidth = {"type": "bandwidth", "capabilities": {"bandwidth": True}}
    assert [p for p in patched if p["type"] == "bandwidth"] == [bandwidth]
    if bandwidth in original:
        assert [p["type"] for p in patched] == [p["type"] for p in original]
    else:
        assert patched[:-1] == [
            {**p, "ipam": {**p["ipam"], "assign_ipv4": "true", "assign_ipv6": "false"}}
            if p["type"] == "calico"
            else p
            for p in original
        ]
        assert patched[-1] == bandwidth
