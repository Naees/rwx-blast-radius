"""Measure fresh processes against generated CSVs. Input pages may be cached by the OS."""

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def measure(binary, data, env):
    with tempfile.TemporaryDirectory(dir=binary.parent) as folder:
        start = time.perf_counter()
        with subprocess.Popen([str(binary), str(data), folder], env=env) as process:
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, process.args)
        elapsed = time.perf_counter() - start
    rss_mib = usage.ru_maxrss / (1024 ** (2 if sys.platform == "darwin" else 1))
    return elapsed, rss_mib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("--compare", type=Path, help="alternate with this executable on the same data")
    parser.add_argument("--runs", type=int, default=3, help="repetitions per executable")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if args.runs < 1 or args.threads < 1:
        parser.error("runs and threads must be positive")
    env = dict(os.environ, NCPU=str(args.threads))
    if args.profile:
        env["SHOP_PROFILE"] = "1"
    candidates = [args.binary.resolve()]
    if args.compare:
        candidates.append(args.compare.resolve())
    measurements = {candidate: [] for candidate in candidates}
    for i in range(args.runs):
        for candidate in candidates if i % 2 == 0 else reversed(candidates):
            seconds, rss = measure(candidate, args.data.resolve(), env)
            measurements[candidate].append((seconds, rss))
            print(f"{candidate.name} run {i+1}: {seconds:.4f} s, peak RSS {rss:.0f} MiB", flush=True)
    for candidate, results in measurements.items():
        times, peaks = zip(*results)
        print(f"{candidate.name} median {statistics.median(times):.4f} s, peak RSS {max(peaks):.0f} MiB")


if __name__ == "__main__":
    main()
