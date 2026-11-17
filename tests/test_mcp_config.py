import tomllib

from friday.mcp_config import load


def _write(tmp_path, text):
    p = tmp_path / "mcp.toml"
    p.write_text(text)
    return p


def test_extra_server_with_env_from(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTION_KEY", 'secret "value"')
    r = load(_write(tmp_path, '[servers.n]\ncommand = "uvx"\nargs = ["mcp-notion"]\nenv_from = ["NOTION_KEY", "MISSING"]\n'))
    assert r.names == ["n"]
    assert 'mcp_servers.n.command="uvx"' in r.overrides
    assert 'mcp_servers.n.default_tools_approval_mode="approve"' in r.overrides
    env = next(o for o in r.overrides if o.startswith("mcp_servers.n.env="))
    assert tomllib.loads("v = " + env.split("=", 1)[1])["v"] == {"NOTION_KEY": 'secret "value"'}
    assert any("MISSING" in p for p in r.problems)


def test_reserved_and_bad_entries_are_skipped(tmp_path):
    r = load(_write(tmp_path, '[servers.github]\nurl = "x"\n[servers.nothing]\nargs = []\n[servers.off]\ncommand = "x"\nenabled = false\n'))
    assert r.names == [] and len(r.problems) == 2
    r = load(_write(tmp_path, "[servers.x\n"))
    assert r.names == [] and r.problems
