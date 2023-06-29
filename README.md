# Calico Charm

Calico is a new approach to virtual networking and network security for containers,
VMs, and bare metal services, that provides a rich set of security enforcement
capabilities running on top of a highly scalable and efficient virtual network fabric.

This charm will deploy Calico, and configure CNI for use with calico, on any principal
charm that implements the [kubernetes-cni][] interface.

This charm is a component of Charmed Kubernetes. For full information,
please visit the [official Charmed Kubernetes docs](https://www.ubuntu.com/kubernetes/docs/charm-calico).

[kubernetes-cni]: https://github.com/juju-solutions/interface-kubernetes-cni

# Developers

## Build charm

```
charmcraft pack
```
