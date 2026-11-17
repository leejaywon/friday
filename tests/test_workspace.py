"""Workspace paths can't escape the workspace."""

from friday import workspace
from friday.bot import unique_path


def test_workspace_paths_refuse_escapes_including_symlinks(cfg, tmp_path):
    ws = cfg.workspace
    (ws / "files" / "inbox").mkdir(parents=True)
    (ws / "files" / "inbox" / "a.txt").write_text("a")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "s.txt").write_text("s")
    (ws / "files" / "out").symlink_to(outside)  # a link leading outside
    (ws / "files" / "s.txt").symlink_to(outside / "s.txt")

    assert workspace.inside(ws, "files/inbox/a.txt") == (ws / "files/inbox/a.txt").resolve()
    for bad in ("..", "../secret/s.txt", "/etc/passwd", "files/out", "files/out/s.txt", "files/s.txt"):
        assert workspace.inside(ws, bad) is None, bad
    assert workspace.entries(ws, "files/out") is None
    assert [e.rel for e in workspace.entries(ws, "files")] == ["files/inbox"]  # links outside not listed
    assert [e.rel for e in workspace.recent(ws)] == ["files/inbox/a.txt"]


def test_files_arriving_together_get_their_own_names(tmp_path):
    names = [unique_path(tmp_path, "120000_voice.ogg").name for _ in range(3)]
    assert names == ["120000_voice.ogg", "120000_voice-2.ogg", "120000_voice-3.ogg"]
