from charmhelpers.core.hookenv import is_leader  # patched
from charmhelpers.core.host import service_running  # patched
from reactive import calico


def test_series_upgrade():
    calico.set_state('upgrade.series.in-progress')
    is_leader.return_value = False
    service_running.return_value = True
    assert calico.status.blocked.call_count == 0
    assert calico.status.waiting.call_count == 0
    assert calico.status.active.call_count == 0
    calico.ready()
    assert calico.status.blocked.call_count == 1
    assert calico.status.waiting.call_count == 0
    assert calico.status.active.call_count == 0
