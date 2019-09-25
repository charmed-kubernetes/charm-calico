# Developing layer-calico

## Installing build dependencies

To install build dependencies:

```
sudo snap install charm --classic
sudo apt install docker.io
sudo usermod -aG docker $USER
```

After running these commands, terminate your shell session and start a new one
to pick up the modified user groups.

## Building the charm

To build the charm:
```
charm build
```

By default, this will build the charm and place it in
`/tmp/charm-builds/calico`.

## Building resources

To build resources:
```
./build-calico-resources.sh
```

This will produce several .tar.gz files that you will need to attach to the
charm when you deploy it.

## Testing

You can test a locally built calico charm by deploying it with Charmed
Kubernetes.

Create a file named `local-calico.yaml` that contains the following (with paths
adjusted to fit your environment):
```
applications:
  calico:
    charm: /tmp/charm-builds/calico
    resources:
      calico: /path/to/layer-calico/calico-amd64.tar.gz
      calico-upgrade: /path/to/layer-calico/calico-upgrade-amd64.tar.gz
```

Then deploy Charmed Kubernetes with your locally built calico charm:

```
juju deploy cs:~containers/kubernetes-calico --overlay local-calico.yaml
```

## Helpful links

* [Getting Started with charm development](https://jaas.ai/docs/getting-started-with-charm-development)
* [Charm tools documentation](https://jaas.ai/docs/charm-tools)
* [Charmed Kubernetes Calico documentation](https://ubuntu.com/kubernetes/docs/cni-calico)
