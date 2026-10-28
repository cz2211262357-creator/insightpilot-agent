from tests.support import prepare_users


def test_owned_postgres_dump_restores_dataset_and_schema_into_new_container(
    real_dependencies, workspace_root
):
    from scripts.backup import backup_container, verify_restore

    dep = real_dependencies
    version, _ = prepare_users(dep)
    directory = workspace_root / ".cache/insightpilot-verification"
    path = backup_container(dep.postgres.get_wrapped_container(), "insightpilot_fixture", directory)
    result = verify_restore(path)
    assert result["verified"] is True
    assert result["orders"] == 156
    assert result["dataset_sha256"] == [version.sha256]
    assert result["schema_version"] == 10
    assert result["distinct_runtime_roles"] == 3
