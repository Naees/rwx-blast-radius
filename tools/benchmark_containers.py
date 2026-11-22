"""Time local AMD64 images in fresh containers on a prewarmed Docker volume."""
import argparse
import contextlib
import datetime as dt
import json
import statistics
import subprocess
import tempfile
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / ".local"


def docker(*args, timeout=180):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


@contextlib.contextmanager
def volume(name):
    docker("volume", "create", "--label", "shopping-sessions.benchmark=local", name)
    try:
        yield name
    finally:
        docker("volume", "rm", name)


def mount(source, target, readonly=False, kind="volume"):
    spec = f"type={kind},source={source},target={target}"
    return ["--mount", spec + (",readonly" if readonly else "")]


def run_helper(image, cpu, memory, mounts, entrypoint, *args):
    ident = docker("create", "--platform", "linux/amd64", "--pull", "never",
                   "--network", "none", "--user", "0:0", "--read-only", "--cpus", str(cpu),
                   "--memory", memory, "--memory-swap", memory,
                   "--security-opt", "no-new-privileges", *mounts,
                   "--entrypoint", entrypoint, image, *args)
    try:
        docker("start", ident)
        status = int(docker("wait", ident, timeout=600))
        if status:
            logs = subprocess.run(["docker", "logs", ident], capture_output=True, text=True)
            raise RuntimeError(f"helper {entrypoint} exited {status}: {logs.stderr}{logs.stdout}")
    finally:
        docker("rm", "-f", ident)


def verify(db, candidate):
    for table in ("user_daily", "campaign_daily"):
        files = str(candidate / table / "*.parquet")
        expected = [col[0] for col in db.execute(f"DESCRIBE {table}").fetchall()]
        actual = [col[0] for col in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [files]).fetchall()]
        if set(actual) != set(expected):
            raise ValueError(f"{table} has unexpected columns: {actual}")
        columns = ", ".join(f'"{name}"' for name in expected)
        wanted = f"SELECT {columns} FROM {table}"
        got = f"SELECT {columns} FROM read_parquet(?)"
        # EXCEPT ALL compares multiplicity as well as nulls and field values.
        for left, right in ((wanted, got), (got, wanted)):
            if db.execute(f"SELECT 1 FROM ({left} EXCEPT ALL {right}) LIMIT 1", [files]).fetchone():
                return False
    return True


def comparison(images, data, oracle, result_dir, rounds, requested_cpus, memory_gib):
    info = json.loads(docker("info", "--format", '{"cpus":{{.NCPU}},"bytes":{{.MemTotal}}}'))
    cpu = min(info["cpus"], requested_cpus)
    memory_mib = min(memory_gib * 1024, info["bytes"] // 2**20)
    memory = f"{memory_mib}m"
    token = "shop-pair-" + uuid.uuid4().hex[:12]
    # Resolve mutable tags before running either image. No image is pulled.
    resolved = {}
    for label, ref in images.items():
        details = json.loads(docker("image", "inspect", ref))[0]
        if (details["Os"], details["Architecture"]) != ("linux", "amd64"):
            raise ValueError(f"{ref} must be a linux/amd64 image")
        resolved[label] = details["Id"]
    helper = next(iter(resolved.values()))
    report = dict(images=resolved, source=str(data), oracle=str(oracle) if oracle else None,
                  vm=info, cpus=cpu, memory_limit=memory, runs=[])
    report_file = result_dir / "results.json"

    def save():
        report_file.write_text(json.dumps(report, indent=2) + "\n")

    save()
    with tempfile.TemporaryDirectory(dir=result_dir) as tmp, volume(token + "-data") as input_vol:
        tmp = Path(tmp)
        input_mount = mount(input_vol, "/data", True)
        run_helper(helper, cpu, memory, mount(data, "/source", True, "bind") + mount(input_vol, "/data"),
                   "/bin/cp", "-R", "/source/.", "/data")
        labels = list(resolved)
        for round_no in range(1, rounds + 1):
            pivot = (round_no - 1) % len(labels)
            order = labels[pivot:] + labels[:pivot]
            for label in order:
                name = f"{token}-{round_no}-{label}"
                dest = tmp / f"{round_no}-{label}"
                dest.mkdir()
                with volume(name + "-out") as out, volume(name + "-scratch") as scratch:
                    writable = mount(out, "/output") + mount(scratch, "/scratch")
                    # The measured container runs as uid 1000 and has no capabilities.
                    run_helper(helper, cpu, memory, writable, "/bin/chown", "1000:1000", "/output", "/scratch")
                    # Repeat immediately before each trial to minimize differing cache residency.
                    run_helper(helper, cpu, memory, input_mount, "/bin/sh", "-c",
                               "find /data -type f -exec cat {} + > /dev/null")
                    ident = docker("create", "--name", name, "--platform", "linux/amd64",
                                   "--pull", "never", "--read-only", "--user", "1000:1000",
                                   "--network", "none", "--cap-drop", "ALL",
                                   "--security-opt", "no-new-privileges",
                                   "--cpus", str(cpu), "--cpuset-cpus", f"0-{cpu-1}",
                                   "--memory", memory, "--memory-swap", memory,
                                   "--pids-limit", "4096", "--tmpfs", "/tmp:rw,size=64m,nosuid,nodev",
                                   "-e", f"NCPU={cpu}", "-e", f"MEM_GB={memory_mib // 1024}",
                                   "-e", "DATA_DIR=/data", "-e", "OUTPUT_DIR=/output",
                                   "-e", "SCRATCH_DIR=/scratch", "-e", "HOME=/scratch",
                                   *input_mount, *writable, resolved[label])
                    try:
                        docker("start", ident)
                        docker("wait", ident, timeout=300)
                        state = json.loads(docker("inspect", "--format", "{{json .State}}", ident))
                        started, finished = (dt.datetime.fromisoformat(state[k].replace("Z", "+00:00"))
                                             for k in ("StartedAt", "FinishedAt"))
                        row = dict(round=round_no, label=label, started=state["StartedAt"],
                                   finished=state["FinishedAt"], seconds=(finished-started).total_seconds(),
                                   exit=state["ExitCode"], oom=state["OOMKilled"], verified=None)
                        report["runs"].append(row)
                        save()
                        if row["exit"] or row["oom"]:
                            logs = subprocess.run(["docker", "logs", ident], capture_output=True, text=True)
                            raise RuntimeError(f"{name} failed: {state}, {logs.stderr}{logs.stdout}")
                        # docker cp is after the daemon's FinishedAt timestamp.
                        if oracle:
                            docker("cp", f"{ident}:/output/.", str(dest))
                        print(f'{label} {round_no}: {row["seconds"]:.4f}s', flush=True)
                    finally:
                        docker("rm", "-f", ident)

        # Defer the DuckDB comparisons until after the final timed trial.
        if oracle:
            import duckdb
            with duckdb.connect(str(oracle), read_only=True) as db:
                db.execute("SET threads=2")
                db.execute("SET memory_limit='1536MiB'")
                db.execute("SET temp_directory=?", [str(tmp / "spill")])
                for row in report["runs"]:
                    candidate = tmp / f'{row["round"]}-{row["label"]}'
                    row["verified"] = verify(db, candidate)
                    save()
                    if not row["verified"]:
                        raise AssertionError(f"{candidate.name} differs from the reference")
    report["medians"] = {label: statistics.median(row["seconds"] for row in report["runs"]
                                                if row["label"] == label) for label in images}
    save()
    print(json.dumps(report["medians"], indent=2), flush=True)
    print(report_file, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", action="append", required=True, metavar="LABEL=IMAGE")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, help="DuckDB containing independently computed output tables")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cpus", type=int, default=8)
    parser.add_argument("--memory-gib", type=int, default=24)
    args = parser.parse_args()
    images = {}
    for entry in args.entry:
        label, separator, image = entry.partition("=")
        if not separator or not image or not (label.isascii() and label.isalnum()) or label in images:
            parser.error("--entry needs a unique alphanumeric label and an image, such as stream=shop:stream")
        images[label] = image
    if min(args.runs, args.cpus, args.memory_gib) < 1:
        parser.error("runs, cpus and memory-gib must be positive")
    if not (args.data / "manifest.json").is_file() or (args.oracle and not args.oracle.is_file()):
        parser.error("provide a generated dataset and an existing oracle if using --oracle")
    BASE.mkdir(exist_ok=True)
    result_dir = Path(tempfile.mkdtemp(prefix="docker-benchmark-", dir=BASE))
    print(f"Results: {result_dir}", flush=True)
    comparison(images, args.data.resolve(), args.oracle.resolve() if args.oracle else None,
               result_dir, args.runs, args.cpus, args.memory_gib)


if __name__ == "__main__":
    main()
