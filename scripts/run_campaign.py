"""Run a campaign, check sampled concurrency, and save the observed result as JSON."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-url", default="http://127.0.0.1:8000")
    parser.add_argument("--image-url", default="https://firmware.example/2.0.0.bin")
    parser.add_argument("--source-version", default="1.0.0")
    parser.add_argument("--target-version", default="2.0.0")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--failure-threshold", type=float, default=100)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output", default="reports/campaign.json")
    parser.add_argument(
        "--restart-after",
        type=float,
        help="Local Compose only: restart API and worker after this many seconds",
    )
    args = parser.parse_args()
    started = time.monotonic()
    report = {"startedAt": datetime.now(UTC).isoformat(), "samples": [], "restart": None}
    with httpx.Client(base_url=args.controller_url, timeout=30, trust_env=False) as client:
        response = client.post(
            "/campaigns",
            json={
                "targetVersion": args.target_version,
                "imageUrl": args.image_url,
                "deviceFilter": {"currentVersion": args.source_version},
                "maxConcurrent": args.concurrency,
                "failureThresholdPercent": args.failure_threshold,
            },
        )
        response.raise_for_status()
        campaign = response.json()
        report["campaignId"] = campaign["id"]
        while time.monotonic() - started < args.timeout:
            elapsed = time.monotonic() - started
            if (
                args.restart_after is not None
                and elapsed >= args.restart_after
                and report["restart"] is None
            ):
                subprocess.run(
                    ["docker", "compose", "restart", "api", "worker"],
                    cwd=Path(__file__).resolve().parents[1],
                    check=True,
                )
                report["restart"] = {
                    "requestedAtSeconds": round(elapsed, 3),
                    "completedAtSeconds": round(time.monotonic() - started, 3),
                }
            try:
                response = client.get(f"/campaigns/{campaign['id']}")
                response.raise_for_status()
                campaign = response.json()
            except httpx.HTTPError:
                time.sleep(1)
                continue
            assert campaign["slotsHeld"] <= args.concurrency, campaign
            assert sum(campaign["counts"].values()) == campaign["totalDevices"], campaign
            sample = {
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "status": campaign["status"],
                "counts": campaign["counts"],
                "slotsHeld": campaign["slotsHeld"],
            }
            report["samples"].append(sample)
            print(json.dumps(sample), flush=True)
            if campaign["status"] in ("COMPLETED", "HALTED"):
                break
            time.sleep(2)
        report["final"] = campaign
        report["elapsedSeconds"] = round(time.monotonic() - started, 3)
        report["maxObservedSlots"] = max(
            (sample["slotsHeld"] for sample in report["samples"]), default=0
        )
        report["finishedAt"] = datetime.now(UTC).isoformat()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Saved {output}")
        if campaign["status"] != "COMPLETED":
            raise SystemExit(
                f"Campaign stopped with status {campaign['status']}; inspect the report"
            )


if __name__ == "__main__":
    main()
