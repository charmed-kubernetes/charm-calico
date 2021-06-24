import logging
import os
import pytest

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
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
        calico_charm=await ops_test.build_charm("."),
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
    await ops_test.model.wait_for_idle(wait_for_active=True, timeout=60 * 60)


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
    # build and deploy bird charm
    bird_charm = await ops_test.build_charm("tests/data/bird-operator")
    await ops_test.model.deploy(bird_charm)
    await ops_test.model.wait_for_idle(wait_for_active=True, timeout=60 * 60)
    # verify test service is not reachable from bird
    retcode, stdout, stderr = await ops_test.run(
        'juju', 'ssh', 'bird/leader', 'curl', '--connect-timeout', '3', service_ip
    )
    assert retcode == 28, "Failed service connection test before BGP config"
    # configure bird to peer with calico
    # configure calico to peer with bird
    # verify test service is reachable from bird
    retcode, stdout, stderr = await ops_test.run(
        'juju', 'ssh', 'bird/leader', 'curl', '--connect-timeout', '3', service_ip
    )
    assert retcode == 0, "Failed service connection test after BGP config"
    # clean up calico config
    # remove bird charm
    await bird_app.destroy()
