"""ROADMAP #76: `/add-dir` mounts."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli import mounts as mn


def test_add_list_remove_and_routes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "sibling api"
    project.mkdir()
    other.mkdir()
    assert "No extra directories" in mn.run_add_dir_command("/add-dir", project)
    out = mn.run_add_dir_command(f'/add-dir "{other}"'.replace('"', ""), project)
    assert out.startswith("Mounted") and "/mnt/sibling-api/" in out
    assert mn.mount_routes(project) == {"/mnt/sibling-api/": other.resolve()}
    with pytest.raises(ValueError, match="already exists"):
        mn.add_mount(project, str(other))
    with pytest.raises(ValueError, match="inside the project"):
        mn.add_mount(project, str(project / "src")) if (
            project / "src"
        ).mkdir() is None else None
    with pytest.raises(ValueError, match="not a directory"):
        mn.add_mount(project, str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="must be letters"):
        mn.add_mount(project, str(tmp_path), name="bad name!")
    assert "Cannot mount" in mn.run_add_dir_command("/add-dir nope", project)
    listing = mn.run_add_dir_command("/add-dir list", project)
    assert "/mnt/sibling-api/" in listing and str(other.resolve()) in listing
    assert "Removed" in mn.run_add_dir_command("/add-dir remove sibling-api", project)
    assert "No mount" in mn.run_add_dir_command("/add-dir remove sibling-api", project)
    mn.run_add_dir_command(f"/add-dir {tmp_path} --name root", project)
    assert mn.load_mounts(project)[0].route == "/mnt/root/"
    mn.mounts_path(project).write_text("garbage", encoding="utf-8")
    assert mn.load_mounts(project) == []
