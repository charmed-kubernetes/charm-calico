#!/bin/bash
set -eux

# This script will fetch binaries and create resource tarballs for use by
# charm-[push|release]. The arm64 binaries are not available upsteram for
# v2.6, so we must build them and host them somewhere ourselves. The steps
# for doing that are documented here:
#
# https://gist.github.com/kwmonroe/9b5f8dac2c17f93629a1a3868b22d671

# Supported calico architectures
arches="amd64 arm64"
calicoctl_version="v3.19.1"
calico_cni_version="v3.19.1"

function fetch_and_validate() {
  # fetch a binary and make sure it's what we expect (executable > 20MB)
  min_bytes=20000000
  location="${1-}"
  if [ -z ${location} ]; then
    echo "$0: Missing location parameter for fetch_and_validate"
    exit 1
  fi

  # remove everything up until the last slash to get the filename
  filename=$(echo "${location##*/}")
  case ${location} in
    http*)
      fetch_cmd="wget ${location} -O ./${filename}"
      ;;
    *)
      fetch_cmd="scp ${location} ./${filename}"
      ;;
  esac
  ${fetch_cmd}

  # Make sure we fetched something big enough
  actual_bytes=$(wc -c < ${filename})
  if [ $actual_bytes -le $min_bytes ]; then
    echo "$0: ${filename} should be at least ${min_bytes} bytes"
    exit 1
  fi

  # Make sure we fetched a binary
  if ! file ${filename} 2>&1 | grep -q 'executable'; then
    echo "$0: ${filename} is not an executable"
    exit 1
  fi
}

for arch in ${arches}; do
  rm -rf resource-build-$arch
  mkdir resource-build-$arch
  pushd resource-build-$arch
  fetch_and_validate \
    https://github.com/projectcalico/calicoctl/releases/download/$calicoctl_version/calicoctl-linux-$arch
  fetch_and_validate \
    https://github.com/projectcalico/cni-plugin/releases/download/$calico_cni_version/calico-$arch
  fetch_and_validate \
    https://github.com/projectcalico/cni-plugin/releases/download/$calico_cni_version/calico-ipam-$arch
  mv calicoctl-linux-$arch calicoctl
  mv calico-$arch calico
  mv calico-ipam-$arch calico-ipam

  chmod +x calicoctl calico calico-ipam
  tar -zcvf ../calico-$arch.tar.gz .

  popd
  rm -rf resource-build-$arch
done

# calico-upgrade resource
for arch in ${arches}; do
  rm -rf resource-build-upgrade
  mkdir resource-build-upgrade
  pushd resource-build-upgrade
  if [ $arch = amd64 ]; then
    fetch_and_validate \
      https://github.com/projectcalico/calico-upgrade/releases/download/v1.0.5/calico-upgrade
    chmod +x calico-upgrade
  elif [ $arch = arm64 ]; then
    # git clone https://github.com/projectcalico/calico-upgrade repo
    # pushd repo
    # git checkout 2de2f7a0f26ef3bb1c2cabf06b2dcbcc2bba1d35  # known good commit
    # make build ARCH=arm64
    # popd
    # mv repo/dist/calico-upgrade-linux-$arch ./calico-upgrade

    # arm64 builds are failing due to an upstream issue:
    # https://github.com/projectcalico/calico-upgrade/issues/42
    # For now, we will pull a previously built binary from the charm store.
    wget https://api.jujucharms.com/charmstore/v5/~containers/calico-698/resource/calico-upgrade-arm64/462 \
      -O calico-upgrade-arm64.tar.gz
    tar -xf calico-upgrade-arm64.tar.gz
    checksum="$(sha256sum calico-upgrade)"
    if [ "$checksum" != "7a07816c26ad19f526ab2f57353043dabd708a48185268b41493e458c59b797d  calico-upgrade" ]; then
      echo 'ERROR: checksum does not match, aborting'
      exit 1
    fi
  else
    echo "Unsupported architecture for calico-upgrade: $arch"
    exit 1
  fi
  tar -zcvf ../calico-upgrade-$arch.tar.gz ./calico-upgrade
  popd
  rm -rf resource-build-upgrade
done

# calico-upgrade arm64
rm -rf resource-build-upgrade-arm64

touch calico-node-image.tar.gz
