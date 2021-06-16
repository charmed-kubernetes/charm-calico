import asyncio
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
