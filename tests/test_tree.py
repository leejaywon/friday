import subprocess

import pytest


def test_show_project_tree_preserves_lookup_and_gitignore(tools, cfg):
    project = cfg.code_dir / "demo"
    src = project / "src"
    src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / ".gitignore").write_text("scratch/\nignored.txt\n")
    (src / "scratch").mkdir()
    (src / "ignored.txt").touch()
    (src / "example.py").touch()

    result = tools.show_tree("demo", "src", 2)
    assert "demo/src/" in result and "example.py" in result
    assert "scratch" not in result and "ignored.txt" not in result
    assert "demo/" in tools.show_tree("demo", "   ")
    assert all(row["kind"] == "html" for row in tools.store.pending_outbox())


def test_show_workspace_and_all_roots(tools, cfg):
    folder = cfg.workspace / "files" / "empty"
    folder.mkdir(parents=True, exist_ok=True)
    (folder.parent / "<draft>.txt").touch()
    (cfg.code_dir / "demo").mkdir(parents=True)
    (cfg.data_dir / "outside").mkdir()

    result = tools.show_tree(location="workspace", path="files", depth=1)
    assert "workspace/files/" in result and "empty/" in result
    [row] = tools.store.pending_outbox()
    assert "&lt;draft&gt;.txt" in row["text"]

    result = tools.show_tree(location="all", depth=3)
    assert "code/" in result and "demo/" in result
    assert "workspace/" in result and "<draft>.txt" in result
    assert "outside" not in result


@pytest.mark.parametrize("path", ["..", "files/../files", "/files", ".hidden", "secrets", "config",
                                 "escape", "linked/child"])
def test_show_tree_rejects_unsafe_paths(tools, cfg, path):
    cfg.workspace.mkdir(exist_ok=True)
    for name in ("files", ".hidden", "secrets", "config"):
        (cfg.workspace / name / "child").mkdir(parents=True)
    (cfg.workspace / "google_token.json").touch()
    (cfg.workspace / "escape").symlink_to(cfg.data_dir, target_is_directory=True)
    (cfg.workspace / "linked").symlink_to(cfg.workspace / "files", target_is_directory=True)

    assert tools.show_tree(location="workspace", path=path).startswith("Folder unavailable")
    assert tools.store.pending_outbox() == []
    result = tools.show_tree(location="workspace", depth=4)
    for name in (".hidden", "secrets", "config", "escape", "linked", "google_token.json"):
        assert name not in result


def test_show_tree_rejects_absolute_paths_and_linked_roots(tools, cfg):
    cfg.code_dir.mkdir()
    cfg.workspace.mkdir(exist_ok=True)
    (cfg.code_dir / "linked").symlink_to(cfg.workspace, target_is_directory=True)
    assert tools.show_tree("linked").startswith("Folder unavailable")
    assert tools.show_tree(location="code", path=str(cfg.code_dir)).startswith("Folder unavailable")
    cfg.workspace.rename(cfg.data_dir / "old-workspace")
    cfg.workspace.symlink_to(cfg.data_dir, target_is_directory=True)
    assert tools.show_tree(location="workspace").startswith("Folder unavailable")
    assert tools.store.pending_outbox() == []
