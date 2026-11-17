import asyncio
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

from friday import jobs
from friday.jobs import RunResult, Runner, agent_env, github_slug, parse_claude, parse_code_args


def test_parse_code_args():
    assert parse_code_args(["friday", "fix", "the", "typo"], "claude") == ("claude", "friday", "fix the typo")
    assert parse_code_args(["Codex", "alice/friday", "README 오타 고쳐줘"], "claude") == (
        "codex", "alice/friday", "README 오타 고쳐줘")
    for bad in (["friday"], ["..", "x"], [".jobs", "x"], ["a/b/c", "x"]):
        with pytest.raises(ValueError):
            parse_code_args(bad, "claude")


def test_github_slug():
    assert github_slug("https://github.com/alice/friday.git") == "alice/friday"
    assert github_slug("git@github-friday:alice/friday.git") == "alice/friday"  # SSH host alias
    assert github_slug("https://gitlab.com/owner/proj.git") is None


def test_agent_env_drops_secrets(monkeypatch):
    for k in ("GITHUB_CODE_TOKEN", "OPENAI_API_KEY", "MY_SECRET", "DB_PASSWORD", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(k, "x")
    env = agent_env()
    assert not {"GITHUB_CODE_TOKEN", "OPENAI_API_KEY", "MY_SECRET", "DB_PASSWORD", "TELEGRAM_BOT_TOKEN"} & set(env)
    assert env["PATH"] == os.environ["PATH"] and env["HOME"] == os.environ["HOME"]


def test_agent_cant_read_other_projects(cfg):
    """Only the job's own project (and its clone) stays readable under ~/code."""
    code = cfg.code_dir
    for d in ("mine", "other", ".jobs/mine-1", ".jobs/other-2"):
        (code / d).mkdir(parents=True)
    denied = jobs.agent_secrets(cfg, keep=(code / ".jobs" / "mine-1", code / "mine"))
    assert code / "other" in denied and code / ".jobs" / "other-2" in denied
    assert cfg.data_dir in denied
    assert not {code / "mine", code / ".jobs" / "mine-1", code / ".jobs"} & set(denied)


def test_parse_claude():
    out = "warning: something\n" + json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": " Fixed it. ", "session_id": "abc"})
    r = parse_claude(out)
    assert (r.text, r.session_id, r.errors) == ("Fixed it.", "abc", [])
    r = parse_claude("Traceback: boom")
    assert r.errors and not r.text


def test_job_lifecycle(store):
    a = store.add_job("friday", "fix the typo", "claude")
    b = store.add_job("owner/proj", "add tests", "codex")
    assert store.next_queued_job()["id"] == a
    store.update_job(a, status="running", branch=f"friday/job-{a}")
    assert store.next_queued_job()["id"] == b

    store.link_job_message(100, a)
    assert store.job_for_message(100) == a and store.job_for_message(999) is None
    store.link_job_message(100, b)  # a message belongs to one job
    assert store.job_for_message(100) == b

    assert store.fail_interrupted_jobs() == [a]
    assert store.job(a)["status"] == "failed" and store.job(b)["status"] == "queued"
    with pytest.raises(ValueError):  # field names go into SQL: only known ones
        store.update_job(a, **{"status = 'merged', task": "x"})


# --- Runner, end to end against a local "GitHub" ---------------------------------

def _git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


def _out(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def git_isolated(monkeypatch):
    # Ignore the machine's git config (signing, hooks, default branch names).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path, git_isolated):
    """tmp/remote/github.com/owner/proj.git (bare, branch main, one commit) cloned to tmp/code/proj.
    The path contains github.com so github_slug takes it for a GitHub remote."""
    bare = tmp_path / "remote" / "github.com" / "owner" / "proj.git"
    bare.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("# proj\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "first", cwd=seed)
    _git("push", "-q", str(bare), "main", cwd=seed)
    clone = tmp_path / "code" / "proj"
    _git("clone", "-q", str(bare), str(clone))
    return clone


@pytest.fixture
def fake_agent(monkeypatch):
    """Replaces jobs.run_agent: records each call and writes a file in the worktree."""
    calls = []
    sig = inspect.signature(jobs.run_agent)

    async def fake(*args, **kwargs):
        a = sig.bind(*args, **kwargs).arguments
        calls.append(a)
        (Path(a["workdir"]) / f"change{len(calls)}.txt").write_text(a["prompt"])
        return RunResult(text="did it", session_id="s1")

    monkeypatch.setattr(jobs, "run_agent", fake)
    return calls


def test_github_job_end_to_end(cfg, store, repo, fake_agent):
    runner = Runner(cfg, store)
    jid = store.add_job("proj", "add a change file", "claude")

    out = asyncio.run(runner.execute(store.job(jid)))
    assert out.status == "done" and "did it" in out.message and "didn't push" in out.message

    j = store.job(jid)
    wt = Path(j["worktree"])
    assert wt == cfg.code_dir / ".jobs" / f"proj-{jid}" and wt.is_dir()
    assert (j["branch"], j["base"], j["gh_repo"], j["session_id"], j["pr_url"]) == (
        f"friday/job-{jid}", "main", "owner/proj", "s1", None)
    assert _out("rev-list", "--count", "origin/main..HEAD", cwd=wt) == "1"
    assert _out("status", "--porcelain", cwd=wt) == ""
    assert fake_agent[0]["session_id"] is None and fake_agent[0]["prompt"] == "add a change file"
    assert not (repo / "change1.txt").exists()  # the main clone is untouched

    # Follow-up: same worktree, resumed session, only the follow-up as the prompt.
    store.update_job(jid, followup="also add another file")
    asyncio.run(runner.execute(store.job(jid)))
    assert fake_agent[1]["session_id"] == "s1" and fake_agent[1]["prompt"] == "also add another file"
    assert Path(fake_agent[1]["workdir"]) == wt and store.job(jid)["followup"] is None
    assert _out("rev-list", "--count", "origin/main..HEAD", cwd=wt) == "2"

    asyncio.run(runner.cleanup(store.job(jid)))
    assert not wt.exists()
    assert _out("branch", "--list", f"friday/job-{jid}", cwd=repo) == ""


def test_git_hooks_never_run(cfg, store, repo, fake_agent):
    """A hook planted in the clone (e.g. by an agent) doesn't run when Friday commits."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    marker = repo.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    jid = store.add_job("proj", "x", "claude")
    asyncio.run(Runner(cfg, store).execute(store.job(jid)))
    assert not marker.exists()


# --- Runner, local projects (no GitHub remote) ------------------------------------

def test_local_new_project(cfg, store, git_isolated, fake_agent):
    """An unknown name becomes a new git project in ~/code, changed in place, with Undo."""
    runner = Runner(cfg, store)
    jid = store.add_job("myapp", "make a todo page", "claude")
    out = asyncio.run(runner.execute(store.job(jid)))
    path = cfg.code_dir / "myapp"
    assert out.status == "done" and out.undo
    assert Path(fake_agent[0]["workdir"]) == path and (path / "change1.txt").exists()
    j = store.job(jid)
    assert (j["worktree"], j["gh_repo"], j["branch"]) == (str(path), None, "main")
    assert _out("log", "--format=%s", cwd=path).splitlines() == ["make a todo page", "Start myapp"]

    # Follow-up in the same folder; Undo then reverts both job commits.
    store.update_job(jid, followup="add a second file")
    asyncio.run(runner.execute(store.job(jid)))
    assert len(store.job(jid)["commits"].split()) == 2
    asyncio.run(runner.undo(store.job(jid)))
    assert not (path / "change1.txt").exists() and not (path / "change2.txt").exists()
    assert _out("status", "--porcelain", cwd=path) == ""

    asyncio.run(runner.cleanup(store.job(jid)))  # never deletes a local project
    assert path.is_dir()


def test_local_folder_without_git(cfg, store, git_isolated, fake_agent):
    """Existing files are committed as a snapshot first; the user's later edits are saved apart."""
    path = cfg.code_dir / "notes"
    path.mkdir(parents=True)
    (path / "a.txt").write_text("mine\n")
    runner = Runner(cfg, store)
    jid = store.add_job("notes", "tidy up", "codex")
    asyncio.run(runner.execute(store.job(jid)))
    log = _out("log", "--format=%s", cwd=path).splitlines()
    assert log == ["tidy up", "Snapshot of notes before Friday's first job"]

    (path / "a.txt").write_text("mine, edited\n")
    store.update_job(jid, followup="one more")
    asyncio.run(runner.execute(store.job(jid)))
    log = _out("log", "--format=%s", cwd=path).splitlines()
    assert log[:2] == ["one more", f"Save uncommitted work before Friday job #{jid}"]
    asyncio.run(runner.undo(store.job(jid)))
    assert (path / "a.txt").read_text() == "mine, edited\n"  # Undo keeps the user's edit


# --- MCP tool --------------------------------------------------------------------

def test_start_coding_job(tools, store, repo):
    assert tools.start_coding_job("proj", "README 오타 고쳐줘").startswith("Queued coding job #1 on proj (claude)")
    j = store.next_queued_job()
    assert (j["repo"], j["engine"], j["task"]) == ("proj", "claude", "README 오타 고쳐줘")
    assert "Refused" in tools.start_coding_job("proj", "use password: hunter22 to log in")
    assert "isn't a repo name" in tools.start_coding_job("..", "x")
    assert len(store.recent_jobs()) == 1
