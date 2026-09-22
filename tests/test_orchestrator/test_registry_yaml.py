"""M2 registry promotion + M3 second entry: capabilities.yaml validates,
load_default binds both adapters, and `registry check` reports them."""

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from orchestrator import cli
from orchestrator.registry import (
    CAPABILITIES_PATH,
    HUMAN_HANDOFF,
    load_default,
    load_static,
    load_yaml,
)


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


def test_default_file_validates_and_selects_rag() -> None:
    entries = load_yaml()
    assert set(entries) == {"rag_query", "structured_lookup", HUMAN_HANDOFF}
    rag = entries["rag_query"]
    assert rag.output_contract.required_fields == ["answer_markdown", "citations"]
    assert "grounding_coverage_min" in rag.validation_rules
    lookup = entries["structured_lookup"]
    assert lookup.output_contract.required_fields == ["answer_markdown", "records"]
    assert lookup.validation_rules == []  # records are data, not grounded claims


def test_load_default_binds_both_adapters() -> None:
    reg = load_default()
    assert reg.select("faq_howto") == "rag_query"  # declared order: rag first
    assert reg.select("anything") == "rag_query"  # task_types ["*"]
    # M3: the declared fallback is now *runnable* — switch paths are live
    assert reg.fallback_for("rag_query") == "structured_lookup"
    assert reg.fallback_for("structured_lookup") is None  # handoff is not runnable
    assert not reg.has_impl(HUMAN_HANDOFF)


# -- config artifact identity (05d step 3) ------------------------------------
def test_load_default_names_its_artifact(tmp_path: Path) -> None:
    from orchestrator.registry import config_hash_of

    reg = load_default()
    assert reg.config_hash == config_hash_of(CAPABILITIES_PATH)
    # a byte-identical copy is the same artifact...
    twin = tmp_path / "twin.yaml"
    twin.write_bytes(CAPABILITIES_PATH.read_bytes())
    assert config_hash_of(twin) == reg.config_hash
    # ...and *any* byte change moves the hash, comment edits included —
    # drift is reported, not interpreted
    with twin.open("ab") as fh:
        fh.write(b"\n# a single comment\n")
    assert config_hash_of(twin) != reg.config_hash


def test_static_fixture_has_no_artifact_to_name() -> None:
    assert load_static().config_hash is None


def test_missing_required_field_is_named(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "capabilities:\n  - name: x\n    owner: o\n",  # no use_when, etc.
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required fields"):
        load_yaml(bad)


def test_extra_yaml_keys_are_ignored(tmp_path: Path) -> None:
    good = CAPABILITIES_PATH.read_text(encoding="utf-8").replace(
        "capabilities:", "future_field: whatever\ncapabilities:", 1
    )
    path = tmp_path / "ok.yaml"
    path.write_text(good, encoding="utf-8")
    assert "rag_query" in load_yaml(path)


def test_static_fixture_view_is_unchanged() -> None:
    reg = load_static()
    assert reg.select("faq_howto") == "echo"  # golden/tests keep the fake world


def test_registry_check_cli(tmp_path: Path) -> None:
    result = run("--db", str(tmp_path / "r.db"), "registry", "check")
    assert result.exit_code == 0, result.output
    # 05d step 3: the operator-facing line names the artifact
    assert "config_hash=" in result.stdout
