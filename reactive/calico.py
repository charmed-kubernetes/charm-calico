import os
import yaml
import gzip
import traceback
import ipaddress
import calico_upgrade

from conctl import getContainerRuntimeCtl
from socket import gethostname
from subprocess import check_call, check_output, CalledProcessError, STDOUT

from charms.leadership import leader_get, leader_set
from charms.reactive import when, when_not, when_any, set_state, remove_state
from charms.reactive import hook, is_state
from charms.reactive import endpoint_from_flag, endpoint_from_name
from charms.reactive import data_changed, any_file_changed
from charms.reactive import register_trigger
from charmhelpers.core.hookenv import (
    log,
    resource_get,
    network_get,
    unit_private_ip,
    is_leader,
    local_unit,
    config as charm_config,
    atexit,
    env_proxy_settings
)
from charmhelpers.core.host import (
    arch,
    service,
    service_restart,
    service_running
)
from charmhelpers.core.templating import render
from charms.layer import kubernetes_common, status
from charms.layer.kubernetes_common import kubectl

# TODO:
#   - Handle the 'stop' hook by stopping and uninstalling all the things.

os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')

try:
    CTL = getContainerRuntimeCtl()
    set_state('calico.ctl.ready')
except RuntimeError:
    log(traceback.format_exc())
    remove_state('calico.ctl.ready')

CALICOCTL_PATH = '/opt/calicoctl'
ETCD_KEY_PATH = os.path.join(CALICOCTL_PATH, 'etcd-key')
ETCD_CERT_PATH = os.path.join(CALICOCTL_PATH, 'etcd-cert')
ETCD_CA_PATH = os.path.join(CALICOCTL_PATH, 'etcd-ca')
CALICO_UPGRADE_DIR = '/opt/calico-upgrade'

register_trigger(
    when="cni.kubeconfig.changed", clear_flag="calico.service.installed"
)


@hook('upgrade-charm')
def upgrade_charm():
    remove_state('calico.binaries.installed')
    remove_state('calico.cni.configured')
    remove_state('calico.service.installed')
    remove_state('calico.pool.configured')
    remove_state('calico.npc.deployed')
    remove_state('calico.image.pulled')
    remove_state('calico.bgp.globals.configured')
    remove_state('calico.node.configured')
    remove_state('calico.bgp.peers.configured')
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
    cni = endpoint_from_name('cni')
    cni.manage_flags()


@when('leadership.is_leader', 'leadership.set.calico-v3-data-migration-needed',
      'etcd.available', 'calico.etcd-credentials.installed')
def upgrade_v3_migrate_data():
    status.maintenance('Migrating data to Calico 3')
    try:
        calico_upgrade.configure()
        calico_upgrade.dry_run()
        calico_upgrade.start()
    except Exception:
        log(traceback.format_exc())
        message = 'Calico upgrade failed, see debug log'
        status.blocked(message)
        return
    leader_set({'calico-v3-data-migration-needed': None})


@when('leadership.is_leader')
@when_not('leadership.set.calico-v3-data-migration-needed')
def v3_data_ready():
    leader_set({'calico-v3-data-ready': True})


@when('leadership.is_leader', 'leadership.set.calico-v3-data-ready',
      'leadership.set.calico-v3-npc-cleanup-needed')
def upgrade_v3_npc_cleanup():
    status.maintenance('Cleaning up Calico 2 policy controller')

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
    status.maintenance('Completing Calico 3 upgrade')
    try:
        calico_upgrade.configure()
        calico_upgrade.complete()
        calico_upgrade.cleanup()
    except Exception:
        log(traceback.format_exc())
        message = 'Calico upgrade failed, see debug log'
        status.blocked(message)
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
        status.blocked(message)
        return

    if not archive:
        message = 'Missing calico resource.'
        log(message)
        status.blocked(message)
        return

    filesize = os.stat(archive).st_size
    if filesize < 1000000:
        message = 'Incomplete calico resource'
        log(message)
        status.blocked(message)
        return

    status.maintenance('Unpacking calico resource.')

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
    status.blocked('Waiting for relation to etcd')


@when('etcd.tls.available')
@when_not('calico.etcd-credentials.installed')
def install_etcd_credentials():
    etcd = endpoint_from_flag('etcd.available')
    etcd.save_client_credentials(ETCD_KEY_PATH, ETCD_CERT_PATH, ETCD_CA_PATH)
    # register initial etcd data so that we can detect changes
    data_changed('calico.etcd.data', (etcd.get_connection_string(),
                                      etcd.get_client_credentials()))
    set_state('calico.etcd-credentials.installed')


@when('etcd.tls.available', 'calico.service.installed')
def check_etcd_changes():
    etcd = endpoint_from_flag('etcd.available')
    if data_changed('calico.etcd.data', (etcd.get_connection_string(),
                                         etcd.get_client_credentials())):
        etcd.save_client_credentials(ETCD_KEY_PATH,
                                     ETCD_CERT_PATH,
                                     ETCD_CA_PATH)
        remove_state('calico.service.installed')
        remove_state('calico.npc.deployed')


def get_mtu():
    ''' Get user-specified MTU size, adjusted to make room for encapsulation
    headers. https://docs.projectcalico.org/networking/mtu
    '''
    mtu = charm_config('veth-mtu')
    if not mtu:
        return None

    if charm_config('vxlan') != 'Never':
        return mtu - 50
    elif charm_config('ipip') != 'Never':
        return mtu - 20
    return mtu


def get_bind_address():
    ''' Returns a non-fan bind address for the cni endpoint '''
    try:
        data = network_get('cni')
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


@when('leadership.is_leader', 'leadership.set.calico-v3-data-ready')
@when_not('leadership.set.calico-node-token')
def create_calico_node_token():
    ''' Create the system:calico-node user token '''
    status.maintenance('Creating system:calico-node user token')
    token = kubernetes_common.token_generator()
    user = 'system:calico-node'
    success = kubernetes_common.create_secret(
        token=token,
        username=user,
        user=user
    )
    if not success:
        log('Failed to create system:calico-node user token, will retry')
        status.waiting('Waiting to retry creating calico-node token')
        return
    # create_secret may have added the <user>:: prefix. Get the new token.
    token = kubernetes_common.get_secret_password(user)
    if not token:
        log('Failed to get system:calico-node user token, will retry')
        status.waiting('Waiting to retry creating calico-node token')
        return
    leader_set({'calico-node-token': token})


@when('calico.binaries.installed', 'etcd.available',
      'calico.etcd-credentials.installed', 'cni.kubeconfig.available',
      'leadership.set.calico-node-token', 'leadership.set.calico-v3-data-ready')
@when_not('calico.service.installed')
def install_calico_service():
    ''' Install the calico-node systemd service. '''
    status.maintenance('Installing calico-node service.')

    with open(kubernetes_common.kubeclientconfig_path) as f:
        kubeconfig = yaml.safe_load(f)
    any_file_changed([kubernetes_common.kubeclientconfig_path])
    kubeconfig['users'] = [{
        'name': 'calico-node',
        'user': {
            'token': leader_get('calico-node-token')
        }
    }]
    kubeconfig['contexts'][0]['context']['user'] = 'calico-node'
    with open('/opt/calicoctl/kubeconfig', 'w') as f:
        yaml.dump(kubeconfig, f)

    etcd = endpoint_from_flag('etcd.available')
    service_path = os.path.join(os.sep, 'lib', 'systemd', 'system',
                                'calico-node.service')
    ip_versions = {net.version for net in get_networks(charm_config('cidr'))}
    ip4 = get_bind_address() if 4 in ip_versions else "none"
    ip6 = "autodetect" if 6 in ip_versions else "none"
    render('calico-node.service', service_path, {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'nodename': gethostname(),
        # specify IP so calico doesn't grab a silly one from, say, lxdbr0
        'ip': ip4,
        'ip6': ip6,
        'mtu': get_mtu(),
        'calico_node_image': charm_config('calico-node-image'),
        'ignore_loose_rpf': charm_config('ignore-loose-rpf'),
        'lc_all': os.environ.get('LC_ALL', 'C.UTF-8'),
        'lang': os.environ.get('LANG', 'C.UTF-8')
    })
    check_call(['systemctl', 'daemon-reload'])
    service_restart('calico-node')
    service('enable', 'calico-node')
    remove_state('cni.kubeconfig.changed')
    set_state('calico.service.installed')


@when('config.changed.veth-mtu')
def configure_mtu():
    remove_state('calico.service.installed')
    remove_state('calico.cni.configured')


@when('config.changed.ignore-loose-rpf')
def ignore_loose_rpf_changed():
    remove_state('calico.service.installed')


@when('calico.binaries.installed', 'etcd.available',
      'calico.etcd-credentials.installed',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.pool.configured')
def configure_calico_pool():
    ''' Configure Calico IP pool. '''
    config = charm_config()
    if not config['manage-pools']:
        log('Skipping pool configuration')
        set_state('calico.pool.configured')
        return

    status.maintenance('Configuring Calico IP pool')

    try:
        # remove unrecognized pools, and default pool if CIDR doesn't match
        pools = calicoctl_get('pool')['items']

        cidrs = tuple(cidr.strip() for cidr in config['cidr'].split(','))
        names = tuple('ipv{}'.format(get_network(cidr).version)
                      for cidr in cidrs)
        pool_names_to_delete = [
            pool['metadata']['name'] for pool in pools
            if pool['metadata']['name'] not in names
            or pool['spec']['cidr'] not in cidrs
        ]

        for pool_name in pool_names_to_delete:
            log('Deleting pool: %s' % pool_name)
            calicoctl('delete', 'pool', pool_name, '--skip-not-exists')

        for cidr, name in zip(cidrs, names):
            # configure the default pool
            pool = {
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'IPPool',
                'metadata': {
                    'name': name,
                },
                'spec': {
                    'cidr': cidr,
                    'ipipMode': config['ipip'],
                    'vxlanMode': config['vxlan'],
                    'natOutgoing': config['nat-outgoing'],
                }
            }

            calicoctl_apply(pool)
    except CalledProcessError:
        log(traceback.format_exc())
        if config['ipip'] != 'Never' and config['vxlan'] != 'Never':
            status.blocked('ipip and vxlan configs are in conflict')
        else:
            status.waiting('Waiting to retry calico pool configuration')
        return

    set_state('calico.pool.configured')


@when_any('config.changed.ipip', 'config.changed.nat-outgoing',
          'config.changed.cidr', 'config.changed.manage-pools',
          'config.changed.vxlan')
def reconfigure_calico_pool():
    ''' Reconfigure the Calico IP pool '''
    remove_state('calico.pool.configured')


@when('etcd.available', 'cni.is-worker', 'leadership.set.calico-v3-data-ready')
@when_not('calico.cni.configured')
def configure_cni():
    ''' Configure Calico CNI. '''
    status.maintenance('Configuring Calico CNI')
    cni = endpoint_from_flag('cni.is-worker')
    etcd = endpoint_from_flag('etcd.available')
    os.makedirs('/etc/cni/net.d', exist_ok=True)
    ip_versions = {net.version for net in get_networks(charm_config('cidr'))}
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'kubeconfig_path': '/opt/calicoctl/kubeconfig',
        'mtu': get_mtu(),
        'assign_ipv4': 'true' if 4 in ip_versions else 'false',
        'assign_ipv6': 'true' if 6 in ip_versions else 'false',
    }
    render('10-calico.conflist', '/etc/cni/net.d/10-calico.conflist', context)
    config = charm_config()
    cni.set_config(cidr=config['cidr'], cni_conf_file='10-calico.conflist')
    set_state('calico.cni.configured')


@when('etcd.available', 'cni.is-master')
@when_not('calico.cni.configured')
def configure_master_cni():
    status.maintenance('Configuring Calico CNI')
    cni = endpoint_from_flag('cni.is-master')
    config = charm_config()
    cni.set_config(cidr=config['cidr'], cni_conf_file='10-calico.conflist')
    set_state('calico.cni.configured')


@when_any('config.changed.cidr')
def reconfigure_cni():
    remove_state('calico.cni.configured')


@when('etcd.available', 'calico.cni.configured',
      'calico.service.installed', 'leadership.is_leader',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.npc.deployed')
def deploy_network_policy_controller():
    ''' Deploy the Calico network policy controller. '''
    status.maintenance('Deploying network policy controller.')
    etcd = endpoint_from_flag('etcd.available')
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'calico_policy_image': charm_config('calico-policy-image'),
        'etcd_cert_last_modified': os.path.getmtime(ETCD_CERT_PATH)
    }
    render('policy-controller.yaml', '/tmp/policy-controller.yaml', context)
    try:
        kubectl('apply', '-f', '/tmp/policy-controller.yaml')
        set_state('calico.npc.deployed')
    except CalledProcessError as e:
        status.waiting('Waiting for kubernetes')
        log(str(e))


@when('calico.binaries.installed', 'etcd.available',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.bgp.globals.configured')
def configure_bgp_globals():
    status.maintenance('Configuring BGP globals')
    config = charm_config()

    try:
        try:
            bgp_config = calicoctl_get('bgpconfig', 'default')
        except CalledProcessError as e:
            if b'resource does not exist' in e.output:
                log('default BGPConfiguration does not exist')
                bgp_config = {
                    'apiVersion': 'projectcalico.org/v3',
                    'kind': 'BGPConfiguration',
                    'metadata': {
                        'name': 'default'
                    },
                    'spec': {}
                }
            else:
                raise

        spec = bgp_config['spec']
        spec['asNumber'] = config['global-as-number']
        spec['nodeToNodeMeshEnabled'] = config['node-to-node-mesh']
        spec['serviceClusterIPs'] = [
            {'cidr': cidr}
            for cidr in config['bgp-service-cluster-ips'].split()
        ]
        spec['serviceExternalIPs'] = [
            {'cidr': cidr}
            for cidr in config['bgp-service-external-ips'].split()
        ]
        spec['serviceLoadBalancerIPs'] = [
            {'cidr': cidr}
            for cidr in config['bgp-service-loadbalancer-ips'].split()
        ]
        calicoctl_apply(bgp_config)
    except CalledProcessError:
        log(traceback.format_exc())
        status.waiting('Waiting to retry BGP global configuration')
        return

    set_state('calico.bgp.globals.configured')


@when_any('config.changed.global-as-number',
          'config.changed.node-to-node-mesh',
          'config.changed.bgp-service-cluster-ips',
          'config.changed.bgp-service-external-ips',
          'config.changed.bgp-service-loadbalancer-ips')
def reconfigure_bgp_globals():
    remove_state('calico.bgp.globals.configured')


@when('calico.binaries.installed', 'etcd.available',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.node.configured')
def configure_node():
    status.maintenance('Configuring Calico node')

    node_name = gethostname()
    as_number = get_unit_as_number()
    route_reflector_cluster_id = get_route_reflector_cluster_id()

    try:
        node = calicoctl_get('node', node_name)
        node['spec']['bgp']['asNumber'] = as_number
        node['spec']['bgp']['routeReflectorClusterID'] = \
            route_reflector_cluster_id
        calicoctl_apply(node)
    except CalledProcessError:
        log(traceback.format_exc())
        status.waiting('Waiting to retry Calico node configuration')
        return

    set_state('calico.node.configured')


@when_any('config.changed.subnet-as-numbers', 'config.changed.unit-as-numbers',
          'config.changed.route-reflector-cluster-ids')
def reconfigure_node():
    remove_state('calico.node.configured')


@when('calico.binaries.installed', 'etcd.available',
      'leadership.set.calico-v3-data-ready')
@when_not('calico.bgp.peers.configured')
def configure_bgp_peers():
    status.maintenance('Configuring BGP peers')

    peers = []

    # Global BGP peers
    config = charm_config()
    peers += yaml.safe_load(config['global-bgp-peers'])

    # Subnet-scoped BGP peers
    subnet_bgp_peers = yaml.safe_load(config['subnet-bgp-peers'])
    subnets = filter_local_subnets(subnet_bgp_peers)
    for subnet in subnets:
        peers += subnet_bgp_peers[str(subnet)]

    # Unit-scoped BGP peers
    unit_id = get_unit_id()
    unit_bgp_peers = yaml.safe_load(config['unit-bgp-peers'])
    if unit_id in unit_bgp_peers:
        peers += unit_bgp_peers[unit_id]

    # Give names to peers
    safe_unit_name = local_unit().replace('/', '-')
    named_peers = {
        # name must consist of lower case alphanumeric characters, '-' or '.'
        '%s-%s-%s' % (safe_unit_name, peer['address'].replace(':', '-'),
                      peer['as-number']): peer
        for peer in peers
    }

    try:
        node_name = gethostname()
        for peer_name, peer in named_peers.items():
            peer_def = {
                'apiVersion': 'projectcalico.org/v3',
                'kind': 'BGPPeer',
                'metadata': {
                    'name': peer_name,
                },
                'spec': {
                    'node': node_name,
                    'peerIP': peer['address'],
                    'asNumber': peer['as-number']
                }
            }
            calicoctl_apply(peer_def)

        # Delete unrecognized peers
        existing_peers = calicoctl_get('bgppeers')['items']
        existing_peers = [peer['metadata']['name'] for peer in existing_peers]
        peers_to_delete = [
            peer for peer in existing_peers
            if peer.startswith(safe_unit_name + '-')
            and peer not in named_peers
        ]

        for peer in peers_to_delete:
            calicoctl('delete', 'bgppeer', peer)
    except CalledProcessError:
        log(traceback.format_exc())
        status.waiting('Waiting to retry BGP peer configuration')
        return

    set_state('calico.bgp.peers.configured')


@when_any('config.changed.global-bgp-peers', 'config.changed.subnet-bgp-peers',
          'config.changed.unit-bgp-peers')
def reconfigure_bgp_peers():
    remove_state('calico.bgp.peers.configured')


@atexit
def ready():
    preconditions = [
        'calico.service.installed', 'calico.pool.configured',
        'calico.cni.configured', 'calico.bgp.globals.configured',
        'calico.node.configured', 'calico.bgp.peers.configured'
    ]
    if is_state('upgrade.series.in-progress'):
        status.blocked('Series upgrade in progress')
        return
    for precondition in preconditions:
        if not is_state(precondition):
            return
    if is_leader() and not is_state('calico.npc.deployed'):
        status.waiting('Waiting to retry deploying policy controller')
        return
    if not service_running('calico-node'):
        status.waiting('Waiting for service: calico-node')
        return
    status.active('Calico is active')


def calicoctl(*args):
    cmd = ['/opt/calicoctl/calicoctl'] + list(args)
    env = os.environ.copy()
    env.update(get_calicoctl_env())
    try:
        return check_output(cmd, env=env, stderr=STDOUT)
    except CalledProcessError as e:
        log(e.output)
        raise


def set_http_proxy():
    """
    Check if we have any values for
    juju_http*_proxy and apply them.
    """
    juju_environment = env_proxy_settings()
    if juju_environment and not juju_environment.get('disable-juju-proxy'):
        upper = ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']
        lower = list(map(str.lower, upper))
        keys = upper + lower
        for key in keys:
            from_juju = juju_environment.get(key, None)
            if from_juju:
                os.environ[key] = from_juju


@when_not('calico.image.pulled')
@when('calico.ctl.ready')
def pull_calico_node_image():
    image = resource_get('calico-node-image')

    if not image or os.path.getsize(image) == 0:
        status.maintenance('Pulling calico-node image')
        image = charm_config('calico-node-image')
        set_http_proxy()
        CTL.pull(image)
    else:
        status.maintenance('Loading calico-node image')
        unzipped = '/tmp/calico-node-image.tar'
        with gzip.open(image, 'rb') as f_in:
            with open(unzipped, 'wb') as f_out:
                f_out.write(f_in.read())
        CTL.load(unzipped)

    set_state('calico.image.pulled')


@when_any('config.changed.calico-node-image')
def repull_calico_node_image():
    remove_state('calico.image.pulled')
    remove_state('calico.service.installed')


@when('calico.service.installed', 'calico.pool.configured')
def disable_vxlan_tx_checksumming():
    '''Workaround for https://github.com/projectcalico/calico/issues/3145'''
    config = charm_config()

    if config['disable-vxlan-tx-checksumming'] and config['vxlan'] != 'Never':
        cmd = ['ethtool', '-K', 'vxlan.calico', 'tx-checksum-ip-generic',
               'off']
        try:
            check_call(cmd)
        except CalledProcessError:
            msg = 'Waiting to retry disabling VXLAN TX checksumming'
            log(msg)
            status.waiting(msg)


def calicoctl_get(*args):
    args = ['get', '-o', 'yaml', '--export'] + list(args)
    output = calicoctl(*args)
    result = yaml.safe_load(output)
    return result


def calicoctl_apply(data):
    path = '/tmp/calicoctl-apply.yaml'
    with open(path, 'w') as f:
        yaml.dump(data, f)
    calicoctl('apply', '-f', path)


def get_calicoctl_env():
    etcd = endpoint_from_flag('etcd.available')
    env = {}
    env['ETCD_ENDPOINTS'] = etcd.get_connection_string()
    env['ETCD_KEY_FILE'] = ETCD_KEY_PATH
    env['ETCD_CERT_FILE'] = ETCD_CERT_PATH
    env['ETCD_CA_CERT_FILE'] = ETCD_CA_PATH
    return env


def get_unit_as_number():
    config = charm_config()

    # Check for matching unit rule
    unit_id = get_unit_id()
    unit_as_numbers = yaml.safe_load(config['unit-as-numbers'])
    if unit_id in unit_as_numbers:
        as_number = unit_as_numbers[unit_id]
        return as_number

    # Check for matching subnet rule
    subnet_as_numbers = yaml.safe_load(config['subnet-as-numbers'])
    subnets = filter_local_subnets(subnet_as_numbers)
    if subnets:
        subnets.sort(key=lambda subnet: -subnet.prefixlen)
        subnet = subnets[0]
        as_number = subnet_as_numbers[str(subnet)]
        return as_number

    # No AS number specified for this unit.
    return None


def filter_local_subnets(subnets):
    ip_address = get_bind_address()
    ip_address = ipaddress.ip_address(ip_address)  # IP address
    subnets = [ipaddress.ip_network(subnet) for subnet in subnets]
    subnets = [subnet for subnet in subnets if ip_address in subnet]
    return subnets


def get_unit_id():
    return int(local_unit().split('/')[1])


def get_route_reflector_cluster_id():
    config = charm_config()
    route_reflector_cluster_ids = yaml.safe_load(
        config['route-reflector-cluster-ids']
    )
    unit_id = get_unit_id()
    return route_reflector_cluster_ids.get(unit_id)


def get_network(cidr):
    '''Convert a CIDR to a network instance.'''
    return ipaddress.ip_interface(cidr.strip()).network


def get_networks(cidrs):
    '''Convert a comma-separated list of CIDRs to a list of networks.'''
    return [get_network(cidr) for cidr in cidrs.split(',')]
