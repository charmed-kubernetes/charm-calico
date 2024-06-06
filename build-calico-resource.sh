#!/bin/bash
set -eux

# This script will fetch binaries and create resource tarballs for use by
# charm-[push|release]. The arm64 binaries are not available upstream for
# v2.6, so we must build them and host them somewhere ourselves. The steps
# for doing that are documented here:
#
# https://gist.github.com/kwmonroe/9b5f8dac2c17f93629a1a3868b22d671

# Supported calico architectures
arches="amd64 arm64"
calico_version="v3.27.3"

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

wget \
  https://github.com/projectcalico/calico/releases/download/$calico_version/release-$calico_version.tgz
tar -xf release-$calico_version.tgz

for arch in ${arches}; do
  rm -rf resource-build-$arch
  mkdir resource-build-$arch
  pushd resource-build-$arch
  cp ../release-$calico_version/bin/calicoctl/calicoctl-linux-$arch calicoctl

  tar -zcvf ../calico-$arch.tar.gz .

  popd
  rm -rf resource-build-$arch
done

rm -rf release-$calico_version.tgz release-$calico_version
