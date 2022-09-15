import asyncio
import logging
import os
import pytest
import time
import yaml
log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test):
    log.info("Building charm")
    calico_charm = await ops_test.build_charm(".")
    resource_path = ops_test.tmp_path / "charm-resources"
    resource_path.mkdir()
    resource_build_script = os.path.abspath("./build-calico-resource.sh")
    log.info("Building charm resources")
    retcode, stdout, stderr = await ops_test.run(
        resource_build_script,
        cwd=resource_path
    )
    if retcode != 0:
        log.error(f"retcode: {retcode}")
        log.error(f"stdout:\n{stdout.strip()}")
        log.error(f"stderr:\n{stderr.strip()}")
        pytest.fail("Failed to build charm resources")
    bundle = ops_test.render_bundle(
        "tests/data/bundle.yaml",
        calico_charm=calico_charm,
        resource_path=resource_path
    )
    # deploy with Juju CLI because libjuju does not support local resource
    # paths in bundles
    log.info("Deploying bundle")
    retcode, stdout, stderr = await ops_test.run(
        "juju", "deploy", "-m", ops_test.model_full_name, bundle
    )
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
            log.debug(
                await juju_run(
                    unit, "kubectl --kubeconfig /root/.kube/config get all -A"
                )
            )
        raise


async def juju_run(unit, cmd):
    result = await unit.run(cmd)
    code = result.results["Code"]
    stdout = result.results.get("Stdout")
    stderr = result.results.get("Stderr")
    assert code == "0", f"{cmd} failed ({code}): {stderr or stdout}"
    return stdout


async def test_bgp_service_ip_advertisement(ops_test, kubernetes):
    # deploy a test service in k8s (nginx)
    deployment = {
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {
            'name': 'nginx'
        },
        'spec': {
            'selector': {
                'matchLabels': {
                    'app': 'nginx'
                }
            },
            'template': {
                'metadata': {
                    'labels': {
                        'app': 'nginx'
                    }
                },
                'spec': {
                    'containers': [{
                        'name': 'nginx',
                        'image': 'rocks.canonical.com/cdk/nginx:1.18',
                        'ports': [{
                            'containerPort': 80
                        }]
                    }]
                }
            }
        }
    }
    service = {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {
            'name': 'nginx'
        },
        'spec': {
            'selector': {
                'app': 'nginx'
            },
            'ports': [{
                'protocol': 'TCP',
                'port': 80
            }]
        }

    }
    kubernetes.apply_object(deployment)
    kubernetes.apply_object(service)
    service_ip = kubernetes.read_object(service).spec.cluster_ip

    # deploy bird charm
    await ops_test.model.deploy(entity_url="bird", channel="stable", num_units=1)
    await ops_test.model.block_until(
        lambda: "bird" in ops_test.model.applications,
        timeout=60
    )
    await ops_test.model.wait_for_idle(status="active", timeout=60 * 10)

    # configure calico to peer with bird
    k8s_cp = "kubernetes-control-plane"
    k8s_cp_config = await ops_test.model.applications[k8s_cp].get_config()
    bird_app = ops_test.model.applications['bird']
    calico_app = ops_test.model.applications['calico']
    await calico_app.set_config({
        'bgp-service-cluster-ips': k8s_cp_config['service-cidr']['value'],
        'global-bgp-peers': yaml.dump([
            {'address': unit.public_address, 'as-number': 64512}
            for unit in bird_app.units
        ])
    })

    # configure bird to peer with calico
    await bird_app.set_config({
        'bgp-peers': yaml.dump([
            {'address': unit.public_address, 'as-number': 64512}
            for unit in calico_app.units
        ])
    })

    # verify test service is reachable from bird
    deadline = time.time() + 60 * 10
    while time.time() < deadline:
        retcode, stdout, stderr = await ops_test.run(
            'juju', 'ssh', '-m', ops_test.model_full_name, 'bird/leader',
            'curl', '--connect-timeout', '10', service_ip
        )
        if retcode == 0:
            break
    else:
        pytest.fail("Failed service connection test after BGP config")

    # clean up
    await calico_app.set_config({
        'bgp-service-cluster-ips': '',
        'global-bgp-peers': '[]'
    })
    await bird_app.destroy()


async def test_rp_filter_conflict(ops_test):
    unit_number = 0
    await ops_test.juju(
        'ssh', f'calico/{unit_number}', 'sudo sysctl -w net.ipv4.conf.all.rp_filter=2',
        check=True,
        fail_msg="Failed to set rp_filter"
    )

    calico_app = ops_test.model.applications['calico']
    # false is default, change it to true and back to false to trigger config changed
    await calico_app.set_config({
        'ignore-loose-rpf': "true",
    })
    await calico_app.set_config({
        'ignore-loose-rpf': "false",
    })

    unit = calico_app.units[unit_number]

    def blocked():
        return unit.workload_status == 'blocked' and 'ignore-loose-rpf'\
               in unit.workload_status_message

    await ops_test.model.block_until(blocked, timeout=60)
