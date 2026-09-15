from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.infra.database import alembic_config


def test_existing_profiles_keep_json_schema_and_database_rejects_unknown_modes(
    tmp_path,
):
    url = f"sqlite:///{tmp_path / 'modes.db'}"
    config = alembic_config(url)
    command.upgrade(config, "0018_capability_control_plane")
    engine = create_engine(url)
    table = Table("model_profiles", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert().values(
                id="model_" + "a" * 32,
                display_name="Existing",
                provider="openai",
                model_name="existing-model",
                base_url="",
                timeout_seconds=30,
                supports_stream=True,
                supports_structured_output=True,
                enabled=True,
                source="user",
                version=7,
                created_at=datetime(2026, 7, 17),
                updated_at=datetime(2026, 7, 17),
            )
        )
    command.upgrade(config, "head")
    table = Table("model_profiles", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        profile = connection.execute(select(table)).mappings().one()
        assert profile["structured_output_method"] == "json_schema"
        assert profile["version"] == 7
    assert not next(
        column
        for column in inspect(engine).get_columns("model_profiles")
        if column["name"] == "structured_output_method"
    )["nullable"]
    with engine.begin() as connection:
        connection.execute(
            table.update().values(structured_output_method="function_calling")
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE model_profiles SET structured_output_method='auto'")
        )
    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(config, "0018_capability_control_plane")
    with engine.connect() as connection:
        assert (
            connection.execute(select(table.c.structured_output_method)).scalar_one()
            == "function_calling"
        )
    engine.dispose()
