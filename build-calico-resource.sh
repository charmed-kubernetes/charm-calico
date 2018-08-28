#!/bin/bash
set -eux

# Supported calico architectures
arches="amd64 arm64"

# 2.6.x has no binary releases for arm64; fetch them from neander
neander="ubuntu@10.96.66.14"

fetch_cni_plugins() {
  arch=${1:-}
  if [ -z ${arch} ]; then
    echo "Missing arch parameter to fetch_cni_plugins"
    exit 1
  fi

  mkdir temp
  (cd temp
    wget https://github.com/containernetworking/plugins/releases/download/v0.7.1/cni-plugins-${arch}-v0.7.1.tgz
    tar -vxf cni-plugins-${arch}-v0.7.1.tgz
    mv portmap ..
  )
  rm -rf temp
}

for arch in ${arches}; do
  rm -rf resource-build-$arch
  mkdir resource-build-$arch
  pushd resource-build-$arch

  if [ $arch = "amd64" ]; then
    wget https://github.com/projectcalico/calicoctl/releases/download/v1.6.4/calicoctl
    wget https://github.com/projectcalico/cni-plugin/releases/download/v1.11.6/calico
    wget https://github.com/projectcalico/cni-plugin/releases/download/v1.11.6/calico-ipam
  elif [ $arch = "arm64" ]; then
    scp ${neander}:~/go/src/github.com/projectcalico/calicoctl/dist/calicoctl-linux-arm64 ./calicoctl
    scp ${neander}:~/go/src/github.com/projectcalico/cni-plugin/dist/calico ./calico
    scp ${neander}:~/go/src/github.com/projectcalico/cni-plugin/dist/calico-ipam ./calico-ipam
  else
    echo "Can't fetch binaries for $arch"
    exit 1
  fi

  fetch_cni_plugins $arch
  chmod +x calicoctl calico calico-ipam portmap
  tar -vcaf ../calico-$arch.tar.gz .

  popd
  rm -rf resource-build-$arch
done
