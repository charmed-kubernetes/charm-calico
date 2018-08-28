#!/bin/bash
set -eux

# User can specify which arch resource to build as $1 (default to intel)
arch=${1:-}
if [ -z "$arch" ]; then
  arch="amd64"
fi

# 2.6.x has no binary releases for arm64; fetch them from neander
neander="ubuntu@10.96.66.14"

rm -rf resource-build
mkdir resource-build
cd resource-build

case ${arch} in
  amd64)
    wget https://github.com/projectcalico/calicoctl/releases/download/v1.6.4/calicoctl
    wget https://github.com/projectcalico/cni-plugin/releases/download/v1.11.6/calico
    wget https://github.com/projectcalico/cni-plugin/releases/download/v1.11.6/calico-ipam
    ;;
  arm64|aarch64)
    scp ${neander}:~/go/src/github.com/projectcalico/calicoctl/dist/calicoctl-linux-arm64 ./calicoctl
    scp ${neander}:~/go/src/github.com/projectcalico/cni-plugin/dist/calico ./calico
    scp ${neander}:~/go/src/github.com/projectcalico/cni-plugin/dist/calico-ipam ./calico-ipam
    ;;
  *)
    echo "Unknown arch"
    exit 1
    ;;
esac

mkdir temp
(cd temp
  wget https://github.com/containernetworking/plugins/releases/download/v0.7.1/cni-plugins-${arch}-v0.7.1.tgz
  tar -vxf cni-plugins-${arch}-v0.7.1.tgz
  mv portmap ..
)
rm -rf temp

chmod +x calicoctl calico calico-ipam portmap
tar -vcaf ../calico-${arch}.tar.gz .
cd ..
rm -rf resource-build
