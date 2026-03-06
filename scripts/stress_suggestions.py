#!/usr/bin/env python3
"""
Concurrent stress test for /suggestions scan + status polling.

Usage examples:
  python scripts/stress_suggestions.py --base-url http://127.0.0.1:5000 --users 3 --runs 1
  python scripts/stress_suggestions.py --base-url http://127.0.0.1:5000 --users 5 --runs 2 --full-refresh-first
"""

import argparse
import concurrent.futures
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests


@dataclass
class RunResult:
    run_id: int
    ok: bool
    status: str
    duration_s: float
    polls: int
    last_percent: int
    error: str = ""


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def smoke_check_routes(base_url: str, timeout_s: int) -> Dict[str, str]:
    session = requests.Session()
    paths = ["/forge", "/match", "/batch-match", "/suggestions"]
    results = {}
    for path in paths:
        try:
            response = session.get(_url(base_url, path), timeout=timeout_s)
            if 200 <= response.status_code < 400:
                results[path] = f"OK ({response.status_code})"
            else:
                results[path] = f"FAIL ({response.status_code})"
        except Exception as exc:
            results[path] = f"FAIL ({exc})"
    return results


def run_scan_once(
    run_id: int,
    base_url: str,
    full_refresh: bool,
    timeout_s: int,
    poll_interval_s: float,
) -> RunResult:
    session = requests.Session()

    try:
        # Warm session + state cookie
        initial = session.get(_url(base_url, "/suggestions"), timeout=timeout_s)
        if initial.status_code >= 500:
            return RunResult(
                run_id=run_id,
                ok=False,
                status="http_error",
                duration_s=0.0,
                polls=0,
                last_percent=0,
                error=f"GET /suggestions -> {initial.status_code}",
            )
    except Exception as exc:
        return RunResult(
            run_id=run_id,
            ok=False,
            status="request_error",
            duration_s=0.0,
            polls=0,
            last_percent=0,
            error=f"GET /suggestions failed: {exc}",
        )

    action = "scan_full" if full_refresh else "scan"
    try:
        start_resp = session.post(
            _url(base_url, "/suggestions"),
            data={"action": action},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=timeout_s,
        )
    except Exception as exc:
        return RunResult(
            run_id=run_id,
            ok=False,
            status="request_error",
            duration_s=0.0,
            polls=0,
            last_percent=0,
            error=f"POST /suggestions ({action}) failed: {exc}",
        )

    if start_resp.status_code != 200:
        return RunResult(
            run_id=run_id,
            ok=False,
            status="http_error",
            duration_s=0.0,
            polls=0,
            last_percent=0,
            error=f"POST /suggestions ({action}) -> {start_resp.status_code}",
        )

    start_time = time.monotonic()
    polls = 0
    last_percent = 0
    terminal_status: Optional[str] = None
    terminal_error = ""

    while True:
        if (time.monotonic() - start_time) > timeout_s:
            terminal_status = "timeout"
            terminal_error = f"Timed out after {timeout_s}s"
            break

        try:
            status_resp = session.get(
                _url(base_url, "/api/suggestions/scan-status"),
                timeout=timeout_s,
            )
        except Exception as exc:
            terminal_status = "request_error"
            terminal_error = f"GET /api/suggestions/scan-status failed: {exc}"
            break

        polls += 1
        if status_resp.status_code != 200:
            terminal_status = "http_error"
            terminal_error = f"GET /api/suggestions/scan-status -> {status_resp.status_code}"
            break

        payload = status_resp.json() or {}
        status = payload.get("status", "idle")
        progress = payload.get("progress") or {}
        last_percent = int(progress.get("percent", last_percent) or 0)

        if status in ("done", "error", "idle"):
            terminal_status = status
            terminal_error = payload.get("error") or ""
            break

        time.sleep(poll_interval_s)

    duration = time.monotonic() - start_time
    ok = terminal_status == "done"
    return RunResult(
        run_id=run_id,
        ok=ok,
        status=terminal_status or "unknown",
        duration_s=duration,
        polls=polls,
        last_percent=last_percent,
        error=terminal_error,
    )


def summarize(results: List[RunResult]) -> str:
    total = len(results)
    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    durations = [r.duration_s for r in passed]

    lines = []
    lines.append("")
    lines.append("=== Suggestions Stress Summary ===")
    lines.append(f"Runs: {total}")
    lines.append(f"Passed (status=done): {len(passed)}")
    lines.append(f"Failed: {len(failed)}")

    if durations:
        avg = statistics.mean(durations)
        p95 = sorted(durations)[max(0, int(len(durations) * 0.95) - 1)]
        lines.append(f"Duration avg: {avg:.1f}s")
        lines.append(f"Duration p95: {p95:.1f}s")
        lines.append(f"Duration max: {max(durations):.1f}s")

    if failed:
        lines.append("")
        lines.append("Failures:")
        for result in failed:
            lines.append(
                f"- run={result.run_id} status={result.status} polls={result.polls} "
                f"last_percent={result.last_percent} error={result.error}"
            )

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress test suggestions scan route")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Web app base URL")
    parser.add_argument("--users", type=int, default=3, help="Concurrent users")
    parser.add_argument("--runs", type=int, default=1, help="Runs per user")
    parser.add_argument("--timeout", type=int, default=2400, help="Per-run timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Status polling interval in seconds")
    parser.add_argument("--stagger", type=float, default=0.5, help="Seconds between scheduling runs")
    parser.add_argument(
        "--full-refresh-first",
        action="store_true",
        help="First run uses action=scan_full; all others use action=scan",
    )
    parser.add_argument(
        "--skip-smoke-check",
        action="store_true",
        help="Skip route availability checks (/forge, /match, /batch-match, /suggestions)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    total_runs = max(1, args.users) * max(1, args.runs)

    if not args.skip_smoke_check:
        print("=== Pre-Scan Route Smoke Check ===")
        smoke = smoke_check_routes(args.base_url, timeout_s=30)
        for path, outcome in smoke.items():
            print(f"{path}: {outcome}")

    print("")
    print("=== Starting Suggestions Stress Test ===")
    print(f"base_url={args.base_url}")
    print(f"concurrency={args.users}")
    print(f"total_runs={total_runs}")
    print(f"timeout={args.timeout}s")
    print(f"poll_interval={args.poll_interval}s")
    print("")

    results: List[RunResult] = []
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.users)) as executor:
        for i in range(total_runs):
            run_id = i + 1
            full_refresh = bool(args.full_refresh_first and i == 0)
            futures.append(
                executor.submit(
                    run_scan_once,
                    run_id,
                    args.base_url,
                    full_refresh,
                    args.timeout,
                    args.poll_interval,
                )
            )
            if args.stagger > 0:
                time.sleep(args.stagger)

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"run={result.run_id} status={result.status} ok={result.ok} "
                f"duration={result.duration_s:.1f}s polls={result.polls} last_percent={result.last_percent}"
            )

    results.sort(key=lambda r: r.run_id)
    print(summarize(results))

    if not args.skip_smoke_check:
        print("")
        print("=== Post-Scan Route Smoke Check ===")
        smoke = smoke_check_routes(args.base_url, timeout_s=30)
        for path, outcome in smoke.items():
            print(f"{path}: {outcome}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
