import json

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
