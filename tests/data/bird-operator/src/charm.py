#!/usr/bin/env python3
import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus
from subprocess import check_call
import yaml

log = logging.getLogger(__name__)
bird_config_base = """
log syslog all;
debug protocols all;

protocol kernel {
  persist;
  scan time 20;
  export all;
}

protocol device {
  scan time 10;
}
"""
bird_config_peer = """
protocol bgp {
  import all;
  local as %s;
  neighbor %s as %s;
  direct;
}
"""


class BirdCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.config_changed, self.config_changed)

    def install(self, event):
        self.unit.status = MaintenanceStatus("Installing BIRD")
        check_call(['apt-get', 'update'])
        check_call(['apt-get', 'install', '-y', 'bird'])

    def config_changed(self, event):
        self.unit.status = MaintenanceStatus("Configuring BIRD")
        as_number = self.config['as-number']
        bird_config = "\n".join([bird_config_base] + [
            bird_config_peer % (as_number, peer['address'], peer['as-number'])
            for peer in yaml.safe_load(self.config['bgp-peers'])
        ])
        with open('/etc/bird/bird.conf', 'w') as f:
            f.write(bird_config)
        check_call(['systemctl', 'reload', 'bird'])
        self.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(BirdCharm)
