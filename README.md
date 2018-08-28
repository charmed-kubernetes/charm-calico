# Calico Charm

Calico is a new approach to virtual networking and network security for containers,
VMs, and bare metal services, that provides a rich set of security enforcement
capabilities running on top of a highly scalable and efficient virtual network fabric.

This charm will deploy calico as a background service, and configure CNI for
use with calico, on any principal charm that implements the [kubernetes-cni][]
interface.

[kubernetes-cni]: https://github.com/juju-solutions/interface-kubernetes-cni

## Usage

The calico charm is a [subordinate][]. This charm will require a principal charm
that implements the `kubernetes-cni` interface in order to properly deploy.

[subordinate]: https://docs.jujucharms.com/2.4/en/authors-subordinate-applications
```
juju deploy cs:~containers/calico
juju deploy cs:~containers/etcd
juju deploy cs:~containers/kubernetes-master
juju deploy cs:~containers/kubernetes-worker
juju add-relation calico etcd
juju add-relation calico kubernetes-master
juju add-relation calico kubernetes-worker
```

## Further information

- [Calico Homepage](https://www.projectcalico.org/)
