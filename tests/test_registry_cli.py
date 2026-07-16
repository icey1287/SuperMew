import json

import backend.tools.registry_cli as registry_cli
from backend.tools.registry_cli import main


def test_registry_cli_validates_and_lists_safe_catalog(capsys):
    assert main(["validate"]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    assert validation["tool_count"] >= 4
    assert len(validation["tool_catalog_hash"]) == 64

    assert main(["list-skills", "--role", "user"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in catalog["skills"]] == ["knowledge-base"]
    assert "instructions" not in catalog["skills"][0]


def test_registry_cli_describes_pinned_skill_without_secret_values(capsys):
    assert main(["describe-skill", "knowledge-base", "--role", "user"]) == 0
    described = json.loads(capsys.readouterr().out)

    assert described["name"] == "knowledge-base"
    assert len(described["content_hash"]) == 64
    assert "search_knowledge_base" in described["allowed_tools"]
    assert "# Knowledge Base" in described["instructions"]


def test_registry_cli_requires_explicit_private_data_policy(monkeypatch, capsys):
    monkeypatch.setattr(
        registry_cli,
        "configured_secret_names",
        lambda _registry: frozenset({"SQL_ASSISTANT_DSN"}),
    )

    assert main(["list-tools", "--role", "admin"]) == 0
    default_tools = json.loads(capsys.readouterr().out)["tools"]
    assert "sql_query" not in {item["name"] for item in default_tools}

    assert (
        main(
            [
                "list-tools",
                "--role",
                "admin",
                "--secret-name",
                "SQL_ASSISTANT_DSN",
                "--network-policy",
                "private-data",
            ]
        )
        == 0
    )
    private_tools = json.loads(capsys.readouterr().out)["tools"]
    assert {"sql_schema", "sql_query"}.issubset(
        {item["name"] for item in private_tools}
    )
