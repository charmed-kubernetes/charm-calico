#!/bin/sh
set -eux

rm -rf resource-build
mkdir resource-build
cd resource-build
wget https://github.com/projectcalico/calico-containers/releases/download/v0.23.0/calicoctl
wget https://github.com/projectcalico/calico-cni/releases/download/v1.4.3/calico
wget https://github.com/projectcalico/calico-cni/releases/download/v1.4.3/calico-ipam
chmod +x calicoctl calico calico-ipam
tar -vcaf ../calico-resource.tar.gz .
cd ..
rm -rf resource-build
