"""Run or monitor a campaign and save observed progress, including failed runs."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-url", default="http://127.0.0.1:8000")
    parser.add_argument("--campaign-id", help="Monitor an existing campaign without creating one")
    parser.add_argument("--image-url", default="https://firmware.example/2.0.0.bin")
    parser.add_argument("--source-version", default="1.0.0")
    parser.add_argument("--target-version", default="2.0.0")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--failure-threshold", type=float, default=100)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output", default="reports/campaign.json")
    parser.add_argument(
        "--halt-drain-seconds",
        type=float,
        default=0,
        help="Observe in-flight work after HALTED for this long; never resume it",
    )
    parser.add_argument(
        "--simulator-summary-url",
        help="Optional diagnostic URL fetched only after campaign monitoring",
    )
    parser.add_argument(
        "--restart-after",
        type=float,
        help="Local Compose only: restart API and worker after this many seconds",
    )
    parser.add_argument("--compose-project", help="Explicit Compose project for restart")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose file for restart; repeat for override files",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.halt_drain_seconds < 0:
        parser.error("timeout must be positive and halt-drain-seconds must be nonnegative")
    if args.restart_after is not None and args.restart_after < 0:
        parser.error("restart-after must be nonnegative")
    if (
        args.restart_after is not None
        and args.controller_url.rstrip("/") != "http://127.0.0.1:8000"
        and not args.compose_project
    ):
        parser.error("--compose-project is required for restarting a non-default controller URL")
    return args


def restart_command(args):
    command = ["docker", "compose"]
    if args.compose_project:
        command.extend(["--project-name", args.compose_project])
    for filename in args.compose_file:
        command.extend(["--file", filename])
    return command + ["restart", "api", "worker"]


def run(args, client, *, clock=time.monotonic, sleep=time.sleep, restart=subprocess.run):
    started = clock()
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "samples": [],
        "restart": None,
        "haltSnapshot": None,
        "final": None,
        "lastObservedAt": None,
        "statusRequestFailures": 0,
        "stopReason": "monitor_timeout",
    }
    last_observed = None
    halted_at = None
    campaign = {"id": args.campaign_id}
    try:
        if args.campaign_id is None:
            response = client.post(
                "/campaigns",
                json={
                    "targetVersion": args.target_version,
                    "imageUrl": args.image_url,
                    "deviceFilter": {"currentVersion": args.source_version},
                    "maxConcurrent": args.concurrency,
                    "failureThresholdPercent": args.failure_threshold,
                },
                timeout=min(30, args.timeout),
            )
            response.raise_for_status()
            campaign = response.json()
            report["final"] = campaign
            last_observed = clock()
            report["lastObservedAt"] = datetime.now(UTC).isoformat()
        report["campaignId"] = campaign["id"]
        while clock() - started < args.timeout:
            elapsed = clock() - started
            if halted_at is not None and elapsed - halted_at >= args.halt_drain_seconds:
                report["stopReason"] = "halt_drain_timeout"
                break
            if (
                args.restart_after is not None
                and elapsed >= args.restart_after
                and report["restart"] is None
            ):
                command = restart_command(args)
                report["restart"] = {"requestedAtSeconds": round(elapsed, 3), "command": command}
                restart(
                    command,
                    cwd=Path(__file__).resolve().parents[1],
                    check=True,
                    timeout=max(0.001, args.timeout - elapsed),
                )
                report["restart"]["completedAtSeconds"] = round(clock() - started, 3)
            remaining = args.timeout - (clock() - started)
            if halted_at is not None:
                remaining = min(
                    remaining, args.halt_drain_seconds - (clock() - started - halted_at)
                )
            if remaining <= 0:
                continue
            try:
                response = client.get(f"/campaigns/{campaign['id']}", timeout=min(30, remaining))
                response.raise_for_status()
                campaign = response.json()
            except httpx.HTTPError as exc:
                report["statusRequestFailures"] += 1
                report["lastRequestError"] = type(exc).__name__
                sleep(min(1, max(0, remaining)))
                continue
            last_observed = clock()
            report["lastObservedAt"] = datetime.now(UTC).isoformat()
            report["final"] = campaign
            sample = {
                "elapsedSeconds": round(clock() - started, 3),
                "status": campaign["status"],
                "counts": campaign["counts"],
                "slotsHeld": campaign["slotsHeld"],
            }
            report["samples"].append(sample)
            print(json.dumps(sample), flush=True)
            if (
                not 0 <= campaign["slotsHeld"] <= campaign["maxConcurrent"]
                or any(value < 0 for value in campaign["counts"].values())
                or sum(campaign["counts"].values()) != campaign["totalDevices"]
            ):
                report["stopReason"] = "invariant_violation"
                break
            if clock() - started >= args.timeout:
                report["stopReason"] = "monitor_timeout"
                break
            if campaign["status"] == "COMPLETED":
                report["stopReason"] = "completed"
                break
            if campaign["status"] == "HALTED":
                if halted_at is None:
                    halted_at = clock() - started
                    report["haltSnapshot"] = campaign
                if args.halt_drain_seconds == 0:
                    report["stopReason"] = "campaign_halted"
                    break
                if campaign["slotsHeld"] == 0:
                    report["stopReason"] = "halt_drain_finished"
                    break
            remaining = args.timeout - (clock() - started)
            if halted_at is not None:
                remaining = min(
                    remaining, args.halt_drain_seconds - (clock() - started - halted_at)
                )
            sleep(min(2, max(0, remaining)))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        report["stopReason"] = "request_or_response_error"
        report["errorType"] = type(exc).__name__
    except (OSError, subprocess.SubprocessError) as exc:
        report["stopReason"] = "restart_error"
        report["errorType"] = type(exc).__name__
    finally:
        if args.simulator_summary_url:
            try:
                response = client.get(args.simulator_summary_url, timeout=30)
                response.raise_for_status()
                report["simulatorSummary"] = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                report["simulatorSummaryError"] = type(exc).__name__
        report["elapsedSeconds"] = round(clock() - started, 3)
        report["finalObservationAgeSeconds"] = (
            None if last_observed is None else round(clock() - last_observed, 3)
        )
        report["maxObservedSlots"] = max(
            (sample["slotsHeld"] for sample in report["samples"]), default=0
        )
        report["finishedAt"] = datetime.now(UTC).isoformat()
        report["successful"] = report["stopReason"] == "completed"
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Saved {output}")
    return report


def main():
    args = arguments()
    with httpx.Client(base_url=args.controller_url, timeout=30, trust_env=False) as client:
        report = run(args, client)
    if not report["successful"]:
        raise SystemExit(
            f"Campaign observation stopped: {report['stopReason']}; inspect the report"
        )


if __name__ == "__main__":
    main()
