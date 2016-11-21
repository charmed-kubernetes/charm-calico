import os
from subprocess import check_call, CalledProcessError

from charms.reactive import when, when_not, set_state
from charmhelpers.core.hookenv import log, status_set, resource_get
from charmhelpers.core.host import service_start
from charmhelpers.core.templating import render

# TODO:
#   - Handle the 'stop' hook by stopping and uninstalling all the things.

CALICOCTL_PATH = '/opt/calicoctl'
ETCD_KEY_PATH = os.path.join(CALICOCTL_PATH, 'etcd-key')
ETCD_CERT_PATH = os.path.join(CALICOCTL_PATH, 'etcd-cert')
ETCD_CA_PATH = os.path.join(CALICOCTL_PATH, 'etcd-ca')

@when_not('calico-cni.apps.installed')
def install_layer_calico_cni():
    ''' Unpack the Calico CNI binaries. '''
    try:
        archive = resource_get('calico-cni')
    except Exception:
        message = 'Error fetching the calico-cni resource.'
        log(message)
        status_set('blocked', message)
        return

    if not archive:
        message = 'Missing calico-cni resource.'
        log(message)
        status_set('blocked', message)
        return

    filesize = os.stat(archive).st_size
    if filesize < 1000000:
        message = 'Incomplete calico-cni resource'
        log(message)
        status_set('blocked', message)
        return

    status_set('maintenance', 'Unpacking calico-cni resource.')

    charm_dir = os.getenv('CHARM_DIR')
    unpack_path = os.path.join(charm_dir, 'files', 'calico-cni')
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

    set_state('calico-cni.apps.installed')


@when('etcd.tls.available')
@when_not('calico-cni.etcd.credentials.installed')
def install_etcd_credentials(etcd):
    etcd.save_client_credentials(ETCD_KEY_PATH, ETCD_CERT_PATH, ETCD_CA_PATH)
    set_state('calico-cni.etcd.credentials.installed')


@when('calico-cni.apps.installed', 'etcd.available',
      'calico-cni.etcd.credentials.installed')
@when_not('calico-cni.calicoctl.service.installed')
def install_calicoctl_service(etcd):
    ''' Install the calicoctl systemd service. '''
    status_set('maintenance', 'Installing calicoctl service.')
    service_path = os.path.join(os.sep, 'lib', 'systemd', 'system', 'calicoctl.service')
    render('calicoctl.service', service_path, {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'etcd_cert_path': ETCD_CERT_PATH
    })
    set_state('calico-cni.calicoctl.service.installed')


@when('calico-cni.calicoctl.service.installed', 'docker.available')
@when_not('calico-cni.calicoctl.service.started')
def start_calicoctl_service():
    ''' Start the calicoctl systemd service. '''
    status_set('maintenance', 'Starting calicoctl service.')
    service_start('calicoctl')
    set_state('calico-cni.calicoctl.service.started')


@when('etcd.available', 'cni.is-worker')
@when_not('calico-cni.conf.installed')
def install_calico_cni_conf(etcd, cni):
    ''' Configure Calico CNI. '''
    status_set('maintenance', 'Configuring Calico CNI')
    os.makedirs('/etc/cni/net.d', exist_ok=True)
    cni_config = cni.get_config()
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH,
        'kubeconfig_path': cni_config['kubeconfig_path']
    }
    render('10-calico.conf', '/etc/cni/net.d/10-calico.conf', context)
    set_state('calico-cni.conf.installed')


@when('etcd.available', 'calico-cni.conf.installed',
      'calico-cni.calicoctl.service.started', 'cni.is-worker')
@when_not('calico-cni.npc.deployed')
def deploy_network_policy_controller(etcd, cni):
    ''' Deploy the Calico network policy controller. '''
    status_set('maintenance', 'Deploying network policy controller.')
    context = {
        'connection_string': etcd.get_connection_string(),
        'etcd_key_path': ETCD_KEY_PATH,
        'etcd_cert_path': ETCD_CERT_PATH,
        'etcd_ca_path': ETCD_CA_PATH
    }
    render('policy-controller.yaml', '/tmp/policy-controller.yaml', context)
    cmd = ['kubectl',
           '--kubeconfig=' + cni.get_config()['kubeconfig_path'],
           'create',
           '-f',
           '/tmp/policy-controller.yaml']
    try:
        check_call(cmd)
        set_state('calico-cni.npc.deployed')
    except CalledProcessError as e:
        log(str(e))
