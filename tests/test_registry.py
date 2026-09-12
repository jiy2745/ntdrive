from pathlib import Path

from ntdrive.core.registry import load_builtin_tools

EXPECTED = {
    "vm_list",
    "vm_state",
    "vm_start",
    "vm_stop",
    "vm_reboot",
    "vm_suspend",
    "vm_resume",
    "snap_list",
    "snap_take",
    "snap_revert",
    "snap_delete",
    "kd_setup_host",
    "kd_setup_guest",
    "kd_attach",
    "kd_detach",
    "kd_break",
    "kd_go",
    "kd_exec",
    "kd_wait_event",
    "kd_state",
    "kd_log_tail",
    "term_open",
    "term_send",
    "term_read",
    "term_exec",
    "term_resize",
    "term_close",
    "term_list",
    "con_screenshot",
    "file_push",
    "file_pull",
    "sys_state",
    "sys_health",
}


def test_all_prd_tools_registered() -> None:
    registry = load_builtin_tools()
    assert set(registry.names()) >= EXPECTED


def test_specs_are_well_formed() -> None:
    registry = load_builtin_tools()
    for spec in registry:
        schema = spec.input_schema()
        assert schema["type"] == "object"
        fields = spec.params.model_fields
        for name in spec.positional:
            assert name in fields, f"{spec.name}: positional {name} is not a parameter"
        assert spec.description
        assert spec.group in {"vm", "snap", "kd", "term", "con", "file", "sys"}


def test_destructive_tools_have_confirm() -> None:
    registry = load_builtin_tools()
    for spec in registry:
        if spec.destructive:
            assert "confirm" in spec.params.model_fields, spec.name


READ_ONLY = {
    "vm_list",
    "vm_state",
    "snap_list",
    "kd_wait_event",
    "kd_state",
    "kd_log_tail",
    "term_read",
    "term_list",
    "con_screenshot",
    "sys_state",
    "sys_health",
}


def test_every_tool_states_its_effect_and_describes_every_parameter() -> None:
    registry = load_builtin_tools()
    assert {s.name for s in registry if s.effect == "read"} == READ_ONLY
    for spec in registry:
        if spec.destructive:  # confirm-gated tools are destructive by definition
            assert spec.effect == "destructive", spec.name
        if spec.effect == "read":
            assert "confirm" not in spec.params.model_fields, spec.name
        for name, prop in spec.input_schema()["properties"].items():
            assert prop.get("description"), f"{spec.name}.{name} has no description"


def test_readme_tool_table_is_current() -> None:
    # The table is generated from the registry (scripts/tools_table.py --write README.md).
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    start, end = "<!-- tools:start -->\n", "<!-- tools:end -->"
    block = readme[readme.index(start) + len(start) : readme.index(end)]
    assert block == load_builtin_tools().markdown_table()
