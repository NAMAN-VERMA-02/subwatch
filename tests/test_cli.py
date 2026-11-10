import json
import socket
from datetime import date

import pytest

from subwatch import __version__, cli
from subwatch.cli import main
from subwatch.synthetic import generate

TODAY = "2026-11-09"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBWATCH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: None)
    return tmp_path / "home"


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    folder = tmp_path_factory.mktemp("exports")
    generate(folder, date.fromisoformat(TODAY), noise=0)
    return folder


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_demo_runs_offline(home, tmp_path, monkeypatch, capsys):
    def no_network(*a, **k):
        raise AssertionError("subwatch tried to use the network")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    out = tmp_path / "dash.html"
    assert main(["demo", "--no-open", "--today", TODAY, "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "Demo: synthetic data" in page and "Saved by cancelling" in page
    text = capsys.readouterr().out
    assert "You chose to cancel ChatGPT" in text
    assert not home.exists()  # the demo never touches your real state


def test_scan_is_incremental_and_report_works(home, data, tmp_path, capsys):
    assert main(["scan", str(data)]) == 0
    first = (home / "events.jsonl").read_text(encoding="utf-8").count("\n")
    assert first > 100
    assert main(["scan", str(data)]) == 0
    assert (home / "events.jsonl").read_text(encoding="utf-8").count("\n") == first
    assert "0 new events" in capsys.readouterr().out

    dash = tmp_path / "d.html"
    assert main(["report", "--today", TODAY, "--html", str(dash)]) == 0
    assert "Monthly burn" in capsys.readouterr().out
    assert "<title>subwatch</title>" in dash.read_text(encoding="utf-8")

    cal = tmp_path / "r.ics"
    assert main(["remind", "--today", TODAY, "--ics", str(cal)]) == 0
    assert cal.read_bytes().startswith(b"BEGIN:VCALENDAR\r\n")


def test_review_records_decisions(home, data, monkeypatch, capsys):
    main(["scan", str(data)])
    answers = ["k", "c", "n", "2026-09-01"]

    def scripted(prompt=""):
        if not answers:
            raise EOFError  # what a closed stdin looks like
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", scripted)
    assert main(["review", "--today", TODAY]) == 0
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    choices = [d["choice"] for d in state["decisions"].values()]
    assert choices[:3] == ["keep", "cancel", "unused"]
    assert "2026-09-01" in [d["on"] for d in state["decisions"].values()]


def test_input_eof_in_review_is_not_a_crash(home, data, monkeypatch):
    main(["scan", str(data)])

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main(["review", "--today", TODAY]) == 0


def test_mark(home, capsys):
    assert main(["mark", "Spotify", "cancel", "--on", "2026-11-01"]) == 0
    assert main(["mark", "Notion", "--price", "800"]) == 0
    assert main(["mark", "naman.v@oksbi", "ignore"]) == 0
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["decisions"]["spotify"] == {"choice": "cancel", "on": "2026-11-01"}
    assert state["price"]["notion"] == 80000
    assert state["ignore"] == ["naman v"]
    assert main(["mark", "spotify", "clear"]) == 0
    assert (
        "spotify" not in json.loads((home / "state.json").read_text(encoding="utf-8"))["decisions"]
    )


def test_scan_rejects_missing_paths(home, capsys):
    assert main(["scan", "nope.mbox"]) == 2


def test_rupee_sign_survives_a_cp1252_pipe(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "SUBWATCH_HOME": str(tmp_path)}
    run = subprocess.run(
        [sys.executable, "-m", "subwatch", "mark", "notion", "--price", "119"],
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert "₹119".encode() in run.stdout
    # `> nul` on Windows: NUL reports isatty() True but still encodes as cp1252
    to_null = subprocess.run(
        [sys.executable, "-m", "subwatch", "mark", "notion", "--price", "119"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert to_null.returncode == 0, to_null.stderr
