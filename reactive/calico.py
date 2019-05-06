import os
import traceback
import yaml
from socket import gethostname
from subprocess import check_call, check_output, CalledProcessError

import calico_upgrade
from calico_common import arch
from charms.leadership import leader_get, leader_set
from charms.reactive import when, when_not, when_any, set_state, remove_state
from charms.reactive import hook
from charms.reactive import endpoint_from_flag
from charmhelpers.core import hookenv
from charmhelpers.core.hookenv import log, status_set, resource_get, \
    unit_private_ip, is_leader
from charmhelpers.core.host import service, service_restart, service_running
from charmhelpers.core.templating import render

# TODO:
#   - Handle the 'stop' hook by stopping and uninstalling all the things.

os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')

CALICOCTL_PATH = '/opt/calicoctl'
ETCD_KEY_PATH = os.path.join(CALICOCTL_PATH, 'etcd-key')
ETCD_CERT_PATH = os.path.join(CALICOCTL_PATH, 'etcd-cert')
ETCD_CA_PATH = os.path.join(CALICOCTL_PATH, 'etcd-ca')
CALICO_CIDR = '192.168.0.0/16'
CALICO_UPGRADE_DIR = '/opt/calico-upgrade'


@hook('upgrade-charm')
def upgrade_charm():
    remove_state('calico.binaries.installed')
    remove_state('calico.cni.configured')
    remove_state('calico.service.installed')
    remove_state('calico.pool.configured')
    remove_state('calico.cni.configured')
    remove_state('calico.npc.deployed')
    try:
        log('Deleting /etc/cni/net.d/10-calico.conf')
        os.remove('/etc/cni/net.d/10-calico.conf')
    except FileNotFoundError as e:
        log(e)
    if is_leader() and not leader_get('calico-v3-data-ready'):
        leader_set({
            'calico-v3-data-migration-needed': True,
            'calico-v3-npc-cleanup-needed': True,
            'calico-v3-completion-needed': True
        })


@when('leadership.is_leader', 'leadership.set.calico-v3-data-migration-needed',
      'etcd.available', 'calico.etcd-credentials.installed')
def upgrade_v3_migrate_data():
    status_set('maintenance', 'Migrating data to Calico 3')
    try:
        calico_upgrade.configure()
        calico_upgrade.dry_run()
        calico_upgrade.start()
    except Exception:
        log(traceback.format_exc())
        message = 'Calico upgrade failed, see debug log'
        status_set('blocked', message)
        return
    leader_set({'calico-v3-data-migration-needed': None})


@when('leadership.is_leader')
@when_not('leadership.set.calico-v3-data-migration-needed')
def v3_data_ready():
    leader_set({'calico-v3-data-ready': True})


@when('leadership.is_leader', 'leadership.set.calico-v3-data-ready',
      'leadership.set.calico-v3-npc-cleanup-needed')
def upgrade_v3_npc_cleanup():
    status_set('maintenance', 'Cleaning up Calico 2 policy controller')

    resources = [
        ('Deployment', 'kube-system', 'calico-policy-controller'),
        ('ClusterRoleBinding', None, 'calico-policy-controller'),
        ('ClusterRole', None, 'calico-policy-controller'),
        ('ServiceAccount', 'kube-system', 'calico-policy-controller')
    ]

    for kind, namespace, name in resources:
        args = ['delete', '--ignore-not-found', kind, name]
        if namespace:
            args += ['-n', namespace]
        try:
            kubectl(*args)
        except CalledProcessError:
            log('Failed to cleanup %s %s %s' % (kind, namespace, name))
            return

    leader_set({'calico-v3-npc-cleanup-needed': None})


@when('leadership.is_leader', 'leadership.set.calico-v3-completion-needed',
      'leadership.set.calico-v3-data-ready', 'calico.binaries.installed',
      'calico.service.installed', 'calico.npc.deployed')
@when_not('leadership.set.calico-v3-npc-cleanup-needed')
def upgrade_v3_complete():
    status_set('maintenance', 'Completing Calico 3 upgrade')
    try:
        calico_upgrade.configure()
        calico_upgrade.complete()
        calico_upgrade.cleanup()
    except Exception:
        log(traceback.format_exc())
        message = 'Calico upgrade failed, see debug log'
        status_set('blocked', message)
        return
    leader_set({'calico-v3-completion-needed': None})


@when('leadership.set.calico-v3-data-ready')
@when_not('calico.binaries.installed')
def install_calico_binaries():
    ''' Unpack the Calico binaries. '''
    # on intel, the resource is called 'calico'; other arches have a suffix
    architecture = arch()
    if architecture == "amd64":
        resource_name = 'calico'
    else:
        resource_name = 'calico-{}'.format(architecture)

    try:
        archive = resource_get(resource_name)
    except Exception:
        message = 'Error fetching the calico resource.'
        log(message)
        status_set('blocked', message)
        return

    if not archive:
        message = 'Missing calico resource.'
        log(message)
        status_set('blocked', message)
        return

    filesize = os.stat(archive).st_size
    if filesize < 1000000:
        message = 'Incomplete calico resource'
        log(message)
        status_set('blocked', message)
        return

    status_set('maintenance', 'Unpacking calico resource.')

    charm_dir = os.getenv('CHARM_DIR')
    unpack_path = os.path.join(charm_dir, 'files', 'calico')
    os.makedirs(unpack_path, exist_ok=True)
    cmd = ['tar', 'xfz', archive, '-C', unpack_path]
    log(cmd)
    check_call(cmd)

    apps = [
        {'name': 'calicoctl', 'path': CALICOCTL_PATH},
        {'name': 'calico', 'path': '/opt/cni/bin'},
        {'name': 'calico-ipam', 'path': '/opt/cni/bin'},
    ]

    for app in apps:
        unpacked = os.path.join(unpack_path, app['name'])
        app_path = os.path.join(app['path'], app['name'])
        install = ['install', '-v', '-D', unpacked, app_path]
        check_call(install)

    calicoctl_path = '/usr/local/bin/calicoctl'
    render('calicoctl', calicoctl_path, {})
    os.chmod(calicoctl_path, 0o775)

    set_state('calico.binaries.installed')


@when('calico.binaries.installed', 'etcd.available')
def update_calicoctl_env():
    env = get_calicoctl_env()
    lines = ['export %s=%s' % item for item in sorted(env.items())]
    output = '\n'.join(lines)
    with open('/opt/calicoctl/calicoctl.env', 'w') as f:
        f.write(output)


@when('calico.binaries.installed')
@when_not('etcd.connected')
def blocked_without_etcd():
    status_set('blocked', 'Waiting for relation to etcd')


@when('etcd.tls.available')
@when_not('calico.etcd-credentials.installed')
def install_etcd_credentials():
    etcd = endpoint_from_flag('etcd.available')
    etcd.save_client_credentials(ETCD_KEY_PATH, ETCD_CERT_PATH, ETCD_CA_PATH)
    set_state('calico.etcd-credentials.installed')


def get_bind_address():
    ''' Returns a non-fan bind address for the cni endpoint '''
    try:
        data = hookenv.network_get('cni')
    except NotImplementedError:
        # Juju < 2.1
        return unit_private_ip()

    if 'bind-addresses' not in data:
        # Juju < 2.3
        return unit_private_ip()

    for bind_address in data['bind-addresses']:
        if bind_address['interfacename'].startswith('fan-'):
            continue
        return bind_address['addresses'][0]['address']

    # If we made it here, we didn't find a non-fan CNI bind-address, which is
    # unexpected. Let's log a message and play it safe.
    log('Could not find a non-fan bind-address. Using private-address.')
    return unit_private_ip()


@when('calico.binaries.installed', 'etcd.available',
      'calico.etcd-credentials.installed',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.service.installed')
def install_calico_service():
    ''' Install the calico-node systemd service. '''
    status_set('maintenance', 'Installing calico-node service.')
    etcd = endpoint_from_flag('etcd.available')
    service_path = os.path.join(os.sep, 'lib', 'systemd', 'system',
                                'calico-node.service')
    render('calico-node.service', service_path, {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'nodename': gethostname(),
        # specify IP so calico doesn't grab a silly one from, say, lxdbr0
        'ip': get_bind_address(),
        'calico_node_image': hookenv.config('calico-node-image')
    })
    check_call(['systemctl', 'daemon-reload'])
    service_restart('calico-node')
    service('enable', 'calico-node')
    set_state('calico.service.installed')


@when('calico.binaries.installed', 'etcd.available',
      'calico.etcd-credentials.installed',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.pool.configured')
def configure_calico_pool():
    ''' Configure Calico IP pool. '''
    status_set('maintenance', 'Configuring Calico IP pool')

    # remove unrecognized pools
    try:
        output = calicoctl('get', 'pool', '-o', 'yaml').decode('utf-8')
    except CalledProcessError:
        log('Failed to get pools')
        status_set('waiting', 'Waiting to retry calico pool configuration')
        return

    pool_data = yaml.safe_load(output)
    pools = [item['metadata']['name'] for item in pool_data['items']]
    pools_to_delete = [pool for pool in pools if pool != 'default']

    for pool in pools_to_delete:
        log('Deleting pool: %s' % pool)
        try:
            calicoctl('delete', 'pool', pool, '--skip-not-exists')
        except CalledProcessError:
            log('Failed to delete pool: %s' % pool)
            status_set('waiting', 'Waiting to retry calico pool configuration')
            return

    # configure the default pool
    config = hookenv.config()
    context = {
        'cidr': CALICO_CIDR,
        'ipip': config['ipip'],
        'nat_outgoing': 'true' if config['nat-outgoing'] else 'false',
    }
    render('pool.yaml', '/tmp/calico-pool.yaml', context)
    try:
        calicoctl('apply', '-f', '/tmp/calico-pool.yaml')
    except CalledProcessError:
        status_set('waiting', 'Waiting to retry calico pool configuration')
        return
    set_state('calico.pool.configured')


@when_any('config.changed.ipip', 'config.changed.nat-outgoing')
def reconfigure_calico_pool():
    ''' Reconfigure the Calico IP pool '''
    remove_state('calico.pool.configured')


@when('etcd.available', 'cni.is-worker', 'leadership.set.calico-v3-data-ready')
@when_not('calico.cni.configured')
def configure_cni():
    ''' Configure Calico CNI. '''
    status_set('maintenance', 'Configuring Calico CNI')
    cni = endpoint_from_flag('cni.is-worker')
    etcd = endpoint_from_flag('etcd.available')
    os.makedirs('/etc/cni/net.d', exist_ok=True)
    cni_config = cni.get_config()
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'kubeconfig_path': cni_config['kubeconfig_path']
    }
    render('10-calico.conflist', '/etc/cni/net.d/10-calico.conflist', context)
    cni.set_config(cidr=CALICO_CIDR)
    set_state('calico.cni.configured')


@when('etcd.available', 'cni.is-master')
@when_not('calico.cni.configured')
def configure_master_cni():
    status_set('maintenance', 'Configuring Calico CNI')
    cni = endpoint_from_flag('cni.is-master')
    cni.set_config(cidr=CALICO_CIDR)
    set_state('calico.cni.configured')


@when('etcd.available', 'calico.cni.configured',
      'calico.service.installed', 'cni.is-worker',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.npc.deployed')
def deploy_network_policy_controller():
    ''' Deploy the Calico network policy controller. '''
    status_set('maintenance', 'Deploying network policy controller.')
    etcd = endpoint_from_flag('etcd.available')
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'calico_policy_image': hookenv.config('calico-policy-image')
    }
    render('policy-controller.yaml', '/tmp/policy-controller.yaml', context)
    try:
        kubectl('apply', '-f', '/tmp/policy-controller.yaml')
        set_state('calico.npc.deployed')
    except CalledProcessError as e:
        status_set('waiting', 'Waiting for kubernetes')
        log(str(e))


@when('calico.service.installed', 'calico.pool.configured',
      'calico.cni.configured')
@when_any('cni.is-master', 'calico.npc.deployed')
def ready():
    if not service_running('calico-node'):
        status_set('waiting', 'Waiting for service: calico-node')
    else:
        status_set('active', 'Calico is active')


def calicoctl(*args):
    cmd = ['/opt/calicoctl/calicoctl'] + list(args)
    env = os.environ.copy()
    env.update(get_calicoctl_env())
    try:
        return check_output(cmd, env=env)
    except CalledProcessError as e:
        log(e.output)
        raise


def kubectl(*args):
    cmd = ['kubectl', '--kubeconfig=/root/.kube/config'] + list(args)
    try:
        return check_output(cmd)
    except CalledProcessError as e:
        log(e.output)
        raise


def get_calicoctl_env():
    etcd = endpoint_from_flag('etcd.available')
    env = {}
    env['ETCD_ENDPOINTS'] = etcd.get_connection_string()
    env['ETCD_KEY_FILE'] = ETCD_KEY_PATH
    env['ETCD_CERT_FILE'] = ETCD_CERT_PATH
    env['ETCD_CA_CERT_FILE'] = ETCD_CA_PATH
    return env
