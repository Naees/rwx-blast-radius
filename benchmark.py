"""Score repeated constrained Docker runs using the organizer's sample scorer."""
import argparse
import subprocess
import sys
from pathlib import Path
from statistics import median

# The delta includes the selector and any probe subprocesses. Docker startup
# and time outside the container are excluded.
TIMER = """
import subprocess, sys
from pathlib import Path
def micros():
    fields = dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
    return int(fields['usage_usec'])
start = micros()
subprocess.run([sys.executable, 'selector.py'], check=True, timeout=180)
print('cpu_us', micros() - start, file=sys.stderr)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(".work/sample/sample"))
    parser.add_argument("--scorer", type=Path, default=Path(".work/sample/score.py"))
    parser.add_argument("--image", default="rwx-blast-radius")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    data = args.data.resolve()
    flags = ["--rm", "--platform", args.platform, "--cpus", "2", "--memory", "4096m",
             "--memory-swap", "4096m", "--pids-limit", "256", "--network", "none",
             "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", "65534:65534", "-e", "HOME=/tmp"]
    for name in ["tests.json", "commits"]:
        flags += ["--mount", f"type=bind,source={data/name},target=/data/{name},readonly"]
    totals = []
    for i in range(args.runs):
        run = subprocess.run(["docker", "run", *flags, args.image, "python", "-c", TIMER],
                             capture_output=True, timeout=200)
        logs = run.stderr.decode(errors="replace")
        if run.returncode:
            sys.exit(logs)
        cpu = int(logs.rstrip().splitlines()[-1].removeprefix("cpu_us ")) / 1e6
        path = Path(".work") / f"{data.name}-run-{i+1}.jsonl"
        path.write_bytes(run.stdout)
        report = subprocess.check_output([sys.executable, str(args.scorer), str(data),
                                          str(data / "answers.json"), str(path),
                                          "--cpu-seconds", str(cpu)], text=True)
        print(f"Run {i+1}\n{report}", end="", flush=True)
        totals.append(float(report.split("Total:")[-1].split()[0]))
    print(f"Median {median(totals):.1f} s")


if __name__ == "__main__":
    main()
