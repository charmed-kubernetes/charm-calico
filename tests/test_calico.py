from reactive import calico


def test_series_upgrade():
    assert calico.status_set.call_count == 0
    calico.pre_series_upgrade()
    assert calico.status_set.call_count == 1
