"""Probe inherited GitHub CI detection in a published macOS CLI and live ingestion."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    decoder = json.JSONDecoder()
    for line in path.read_text(encoding="utf-8").splitlines():
        timestamp, event, remaining = line.split(" ", 2)
        properties = {}
        while remaining:
            key, raw = remaining.split("=", 1)
            value, end = decoder.raw_decode(raw)
            properties[key] = value
            remaining = raw[end:].lstrip()
        events.append({"timestamp": timestamp, "event": event, "properties": properties})
    return events


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    if binary.is_dir():
        binary = binary / "opensre"
    assert binary.is_file(), f"Missing packaged executable: {binary}"
    evidence = Path(sys.argv[2]).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    host = {
        "system": platform.system(),
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "CI": os.environ.get("CI"),
        "GITHUB_ACTIONS": os.environ.get("GITHUB_ACTIONS"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        "run_url": (
            f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}"
            f"/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
        ),
    }
    (evidence / "host.json").write_text(json.dumps(host, indent=2) + "\n")
    assert host["system"] == "Darwin", host
    assert host["CI"] == "true" and host["GITHUB_ACTIONS"] == "true", host

    with tempfile.TemporaryDirectory(prefix="opensre-ci-probe-") as directory:
        root = Path(directory)
        env = os.environ.copy()
        # Keep CI and GITHUB_ACTIONS exactly as supplied by the runner. Isolate
        # identity and credentials, enabling only the analytics under test.
        for key in (
            "OPENSRE_ACCOUNT_TOKEN",
            "OPENSRE_WEBAPP_URL",
            "AGENT_USAGE_SECRET",
            "ORGANIZATION_ID",
        ):
            env.pop(key, None)
        env.update(
            {
                "OPENSRE_HOME": str(root),
                "OPENSRE_WIZARD_STORE_PATH": str(root / "opensre.json"),
                "OPENSRE_ACCOUNT_METADATA_PATH": str(root / "account.json"),
                "OPENSRE_DISABLE_KEYRING": "1",
                "OPENSRE_APP_URL": "https://app.opensre.com",
                "OPENSRE_NO_TELEMETRY": "0",
                "OPENSRE_ANALYTICS_DISABLED": "0",
                "OPENSRE_ANALYTICS_LOG_EVENTS": "1",
                "DO_NOT_TRACK": "0",
                "OPENSRE_SENTRY_DISABLED": "1",
                "OPENSRE_LANGFUSE_DISABLED": "1",
                "OPENSRE_IS_TEST": "1",
                "OPENSRE_INSTALL_SOURCE": "github_actions_ci_probe",
            }
        )
        try:
            for label, arguments in (
                ("version", ["--version"]),
                ("install", ["--record-install"]),
                ("command", ["--no-interactive"]),
            ):
                result = subprocess.run(
                    [str(binary), *arguments],
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                (evidence / f"{label}.log").write_text(result.stdout + result.stderr)
                assert result.returncode == 0, f"{label} failed: see {label}.log"
            events = read_events(root / "analytics_events.txt")
            (evidence / "events.json").write_text(json.dumps(events, indent=2) + "\n")
            assert any(e["event"] == "install_detected" for e in events), events
            assert any(e["event"] in {"cli_invoked", "cli_command_opensre"} for e in events), events
            for event in events:
                properties = event["properties"]
                assert properties["is_ci"] is True, event
                assert properties["execution_environment"] == "ci", event
                assert properties["is_container"] is False, event
                assert properties["os_family"].lower() == "darwin", event
                if event["event"] == "install_detected":
                    assert properties["distribution"] == "frozen_binary", event
            # This marker is written only after the ingest endpoint returns 202.
            assert (root / "installed").is_file(), "Live ingestion did not acknowledge install"
            summary = {
                "host": host,
                "anonymous_id": (root / "anonymous_id").read_text().strip(),
                "events": events,
                "install_acknowledged": True,
            }
            (evidence / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary, indent=2))
        finally:
            for name in ("analytics_events.txt", "analytics_errors.log", "anonymous_id"):
                if (root / name).is_file():
                    shutil.copyfile(root / name, evidence / name)


if __name__ == "__main__":
    main()
