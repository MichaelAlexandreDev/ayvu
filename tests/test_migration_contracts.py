import pytest

from ayvu.migration_contracts import MigrationError, MigrationPlan, MigrationResult


def test_ordered_contiguous_version_plan():
    assert MigrationPlan((1, 2, 3), 2).pending(1) == (2,)
    assert MigrationPlan((1,), 1).pending(1) == ()


@pytest.mark.parametrize('versions,target', [((2,), 2), ((1, 3), 3), ((1, 1), 1), ((2, 1), 2), ((True,), 1), ((1,), 2)])
def test_invalid_registry_is_rejected_before_io(versions, target):
    with pytest.raises(MigrationError, match='invalid_registry'):
        MigrationPlan(versions, target)


def test_newer_version_is_not_a_downgrade():
    with pytest.raises(MigrationError, match='newer_schema'):
        MigrationPlan((1,), 1).pending(2)


def test_result_diagnostics_omit_recovery_location():
    result = MigrationResult('failed', 1, 1, error='sql_failure', backup_name='v00000001-to-00000002-000001.sqlite')
    assert not result.ok
    assert 'v00000001' not in repr(result)
