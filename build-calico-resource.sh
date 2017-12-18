#!/bin/sh
set -eux

rm -rf resource-build
mkdir resource-build
cd resource-build
wget https://github.com/projectcalico/calicoctl/releases/download/v1.5.0/calicoctl
wget https://github.com/projectcalico/cni-plugin/releases/download/v1.10.0/calico
wget https://github.com/projectcalico/cni-plugin/releases/download/v1.10.0/calico-ipam
chmod +x calicoctl calico calico-ipam

mkdir temp
(cd temp
  wget https://github.com/containernetworking/plugins/releases/download/v0.6.0/cni-plugins-amd64-v0.6.0.tgz
  tar -vxf cni-plugins-amd64-v0.6.0.tgz
  mv portmap ..
)
rm -rf temp

tar -vcaf ../calico-resource.tar.gz .
cd ..
rm -rf resource-build
