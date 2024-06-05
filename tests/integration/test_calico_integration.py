import asyncio
import logging
import os
import shlex
import time
from pathlib import Path

import juju.application
import pytest
import yaml

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test, k8s_core_bundle, series):
    log.info("Building charm")
    calico_charm = await ops_test.build_charm(".")

    resource_path = ops_test.tmp_path / "charm-resources"
    resource_path.mkdir()
    resource_build_script = os.path.abspath("./build-calico-resource.sh")
    log.info("Building charm resources")
    retcode, stdout, stderr = await ops_test.run(resource_build_script, cwd=resource_path)
    if retcode != 0:
        log.error(f"retcode: {retcode}")
        log.error(f"stdout:\n{stdout.strip()}")
        log.error(f"stderr:\n{stderr.strip()}")
        pytest.fail("Failed to build charm resources")

    log.info("Build Bundle...")
    bundle, *overlays = await ops_test.async_render_bundles(
        k8s_core_bundle,
        Path("tests/data/charm.yaml"),
        calico_charm=calico_charm,
        series=series,
        resource_path=resource_path,
    )

    log.info("Deploying bundle")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} " + " ".join(f"--overlay={f}" for f in overlays)
    retcode, stdout, stderr = await ops_test.run(*shlex.split(cmd))

    if retcode != 0:
        log.error(f"retcode: {retcode}")
        log.error(f"stdout:\n{stdout.strip()}")
        log.error(f"stderr:\n{stderr.strip()}")
        pytest.fail("Failed to deploy bundle")

    try:
        await ops_test.model.wait_for_idle(status="active", timeout=60 * 60)
    except asyncio.TimeoutError:
        k8s_cp = "kubernetes-control-plane"
        assert k8s_cp in ops_test.model.applications
        app = ops_test.model.applications[k8s_cp]
        assert app.units, f"No {k8s_cp} units available"
        unit = app.units[0]
        if "kube-system pod" in unit.workload_status_message:
            log.debug(await juju_run(unit, "kubectl --kubeconfig /root/.kube/config get all -A"))
        raise


async def juju_run(unit, cmd):
    action = await unit.run(cmd)
    await action.wait()
    code = action.results.get("Code", action.results.get("return-code"))
    if code is None:
        log.error(f"Failed to find the return code in {action.results}")
        return -1
    code = int(code)
    stdout = action.results.get("Stdout", action.results.get("stdout")) or ""
    stderr = action.results.get("Stderr", action.results.get("stderr")) or ""
    assert code == 0, f"{cmd} failed ({code}): {stderr or stdout}"
    return stdout


async def test_bgp_service_ip_advertisement(ops_test, kubernetes):
    # deploy a test service in k8s (nginx)
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "nginx"},
        "spec": {
            "selector": {"matchLabels": {"app": "nginx"}},
            "template": {
                "metadata": {"labels": {"app": "nginx"}},
                "spec": {
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "rocks.canonical.com/cdk/nginx:1.18",
                            "ports": [{"containerPort": 80}],
                        }
                    ]
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "nginx"},
        "spec": {"selector": {"app": "nginx"}, "ports": [{"protocol": "TCP", "port": 80}]},
    }
    kubernetes.apply_object(deployment)
    kubernetes.apply_object(service)
    service_ip = kubernetes.read_object(service).spec.cluster_ip

    # deploy bird charm
    await ops_test.model.block_until(lambda: "bird" in ops_test.model.applications, timeout=60)
    await ops_test.model.wait_for_idle(status="active", timeout=60 * 10)

    # configure calico to peer with bird
    k8s_cp = "kubernetes-control-plane"
    k8s_cp_config = await ops_test.model.applications[k8s_cp].get_config()
    bird_app = ops_test.model.applications["bird"]
    calico_app = ops_test.model.applications["calico"]
    await calico_app.set_config(
        {
            "bgp-service-cluster-ips": k8s_cp_config["service-cidr"]["value"],
            "global-bgp-peers": yaml.dump(
                [{"address": unit.public_address, "as-number": 64512} for unit in bird_app.units]
            ),
        }
    )

    # configure bird to peer with calico
    await bird_app.set_config(
        {
            "bgp-peers": yaml.dump(
                [{"address": unit.public_address, "as-number": 64512} for unit in calico_app.units]
            )
        }
    )

    # verify test service is reachable from bird
    deadline = time.time() + 60 * 10
    while time.time() < deadline:
        retcode, stdout, stderr = await ops_test.run(
            "juju",
            "ssh",
            "-m",
            ops_test.model_full_name,
            "bird/leader",
            "curl",
            "--connect-timeout",
            "10",
            service_ip,
        )
        if retcode == 0:
            break
    else:
        pytest.fail("Failed service connection test after BGP config")

    # clean up
    await calico_app.set_config({"bgp-service-cluster-ips": "", "global-bgp-peers": "[]"})


async def get_leader(app: juju.application.Application):
    """Find leader unit of an application."""
    is_leader = await asyncio.gather(*(u.is_leader_from_status() for u in app.units))
    for idx, flag in enumerate(is_leader):
        if flag:
            return idx


@pytest.fixture()
async def ignore_loose_rp_filter(ops_test):
    calico_app: juju.application.Application = ops_test.model.applications["calico"]
    calico_leader = await get_leader(calico_app)
    cmd = "sysctl -w net.ipv4.conf.all.rp_filter={v}"
    try:
        await juju_run(calico_app.units[calico_leader], cmd.format(v=2))
        # false is default, change it to true and back to false to trigger config changed
        await calico_app.set_config({"ignore-loose-rpf": "true"})
        await calico_app.set_config({"ignore-loose-rpf": "false"})
        yield calico_leader
    finally:
        await juju_run(calico_app.units[calico_leader], cmd.format(v=1))
        await calico_app.set_config({"ignore-loose-rpf": "true"})
        await calico_app.set_config({"ignore-loose-rpf": "false"})
        await ops_test.model.wait_for_idle(status="active", timeout=60 * 5)


async def test_rp_filter_conflict(ops_test, ignore_loose_rp_filter):
    unit_number = ignore_loose_rp_filter
    calico_app = ops_test.model.applications["calico"]
    unit = calico_app.units[unit_number]

    def blocked():
        return (
            unit.workload_status == "blocked"
            and "ignore-loose-rpf" in unit.workload_status_message
        )

    await ops_test.model.block_until(blocked, timeout=60)
