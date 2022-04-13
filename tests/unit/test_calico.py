from charmhelpers.core.hookenv import is_leader, config  # patched
from charmhelpers.core.host import service_running  # patched
from reactive import calico
from unittest.mock import patch, mock_open


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
    calico.remove_state('upgrade.series.in-progress')


def test_ignore_loose_rpf_at_exit():
    # Test the case when charm should be blocked
    # i.e. when rp filter == 2 and ignore-loose-rpf is false
    with patch("builtins.open", mock_open(read_data="2")):
        calico.status.reset_mock()
        # config.return value returns the config setting for ignore-loose-rpf
        config.return_value = False
        calico.ready()
        assert calico.status.blocked.call_count == 1

    # Test the case when rp filter != 2 and ignore-loose-rpf is false
    with patch("builtins.open", mock_open(read_data="1")):
        calico.status.reset_mock()
        # config.return value returns the config setting for ignore-loose-rpf
        config.return_value = False
        calico.ready()
        assert calico.status.blocked.call_count == 0

    # Test the case when rp filter == 2 and ignore-loose-rpf is true
    with patch("builtins.open", mock_open(read_data="2")):
        calico.status.reset_mock()
        # config.return value returns the config setting for ignore-loose-rpf
        config.return_value = True
        calico.ready()
        assert calico.status.blocked.call_count == 0

    # Test the case when rp filter != 2 and ignore-loose-rpf is true
    with patch("builtins.open", mock_open(read_data="1")):
        calico.status.reset_mock()
        # config.return value returns the config setting for ignore-loose-rpf
        config.return_value = True
        calico.ready()
        assert calico.status.blocked.call_count == 0
