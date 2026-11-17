"""The local Bot API server is used only when configured; workspace paths can't escape the workspace."""

from dataclasses import replace

from friday import bot as botmod
from friday import workspace


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


def test_local_bot_api_server_only_when_api_id_is_set(cfg):
    cfg = replace(cfg, telegram_token="123:abc", allowed_user_ids=(1,))
    for api_id, base, local in (("", "https://api.telegram.org/bot", False),
                                ("12345", "http://127.0.0.1:8081/bot", True)):
        f = botmod.Friday(replace(cfg, telegram_api_id=api_id))
        try:
            app = f.build()
            assert app.bot.base_url == base + "123:abc"
            assert app.bot.local_mode is local
        finally:
            f.store.close()
