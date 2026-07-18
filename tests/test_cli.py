"""Tests for the YSYL command-line interface."""

from __future__ import annotations

from subprocess import CompletedProcess

import ysyl.cli as cli


def test_ysyl_run_command_matcher_accepts_supported_launchers():
    assert cli._is_ysyl_run_command("ysyl run --no-ui")
    assert cli._is_ysyl_run_command("/usr/local/bin/python3 /usr/local/bin/ysyl run")
    assert cli._is_ysyl_run_command("python3 -u -m ysyl run --interval 5")
    assert cli._is_ysyl_run_command("python3 -m ysyl.cli run")


def test_ysyl_run_command_matcher_rejects_other_commands():
    assert not cli._is_ysyl_run_command("ysyl stopall")
    assert not cli._is_ysyl_run_command("ysyl status")
    assert not cli._is_ysyl_run_command("/bin/sh -c 'echo ysyl run'")
    assert not cli._is_ysyl_run_command("python3 -c 'print(\"ysyl run\")'")


def test_find_ysyl_daemon_pids_filters_to_other_daemon_processes(monkeypatch):
    process_list = """\
  101 /usr/local/bin/python3 /usr/local/bin/ysyl run
  102 python3 -m ysyl run --no-ui
  103 /usr/local/bin/python3 /usr/local/bin/ysyl stopall
  104 /bin/sh -c 'echo ysyl run'
  105 /usr/local/bin/python3 /usr/local/bin/ysyl run
  bad malformed process row
"""
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, stdout=process_list, stderr=""),
    )
    monkeypatch.setattr(cli.os, "getpid", lambda: 105)

    assert cli._find_ysyl_daemon_pids() == [101, 102]


def test_stopall_requests_sigterm_for_each_daemon(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_find_ysyl_daemon_pids", lambda: [101, 202])
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    assert cli.main(["stopall"]) == 0
    assert calls == [(101, cli.signal.SIGTERM), (202, cli.signal.SIGTERM)]
    assert "Requested shutdown for 2 YSYL daemons: 101, 202" in capsys.readouterr().out


def test_stopall_is_idempotent_when_no_daemons_are_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_find_ysyl_daemon_pids", lambda: [])

    assert cli.main(["stopall"]) == 0
    assert capsys.readouterr().out.strip() == "No running YSYL daemons found."


def test_stopall_ignores_already_exited_daemon_and_reports_permission_failures(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_find_ysyl_daemon_pids", lambda: [101, 202])

    def kill(pid: int, sig: int) -> None:
        assert sig == cli.signal.SIGTERM
        if pid == 101:
            raise ProcessLookupError
        raise PermissionError

    monkeypatch.setattr(cli.os, "kill", kill)

    assert cli.main(["stopall"]) == 1
    captured = capsys.readouterr()
    assert "Could not stop YSYL daemon 202: permission denied" in captured.err


def test_stopall_reports_process_listing_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_find_ysyl_daemon_pids", lambda: (_ for _ in ()).throw(RuntimeError("ps failed")))

    assert cli.main(["stopall"]) == 1
    assert "Failed to stop YSYL daemons: ps failed" in capsys.readouterr().err
