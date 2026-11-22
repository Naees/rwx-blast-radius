# Shopping Sessions

A C++17 implementation of the Builders Buildathon Shopping Sessions v5 rules. The executable reads the mounted CSV files and writes `user_daily/` and `campaign_daily/` in Parquet format. It accepts `DATA_DIR`, `OUTPUT_DIR`, and `NCPU`, or input and output directories as positional arguments. Use a dedicated output directory.

The default executable scored **2.1078 seconds** on the organizer’s hidden practice input. It chooses between two exact event readers using the total size of the event CSVs. On the local eight-worker configuration, it chose ordered reads for the 20M-event input and streaming for the 60M-event input. [Measured runtimes, correctness and reproduction commands](BENCHMARK.md)

## Build and run the scoring image

```sh
docker build --platform linux/amd64 -t shopping-sessions-local:v5 .
mkdir -p .local/container-output .local/scratch
chmod 777 .local/container-output .local/scratch
docker run --rm --platform linux/amd64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 \
  --cpus 8 --memory 24g --memory-swap 24g --pids-limit 4096 \
  --tmpfs /tmp:rw,size=64m,mode=1777 \
  -v "$PWD/.local/dev:/data:ro" -v "$PWD/.local/container-output:/output" \
  -v "$PWD/.local/scratch:/scratch" -e HOME=/scratch -e NCPU=8 -e MEM_GB=24 shopping-sessions-local:v5
```

The image selects AVX2 on compatible x86 processors and ships a portable fallback. Apache Arrow and Parquet C++ come from the pinned PyArrow wheel. Python runs only at build time and in the external test tools.

## Reproduce the tests

This checkout keeps the official files, generated datasets, local executable and Python packages under the ignored `.local/` directory. On this checkout, `make fixtures check-dev` is sufficient to rebuild and rerun the official checks. To set up a new checkout, use Python 3.13, a C++17 compiler, GNU Make, and Rust 1.85 or newer.

```sh
mkdir -p .local
python3.13 -m venv .local/venv
.local/venv/bin/pip install pyarrow==21.0.0 polars==1.34.0
base=https://www.joinbuilders.org/buildathon/tracks/shopping-sessions/files
for pair in starter-duckdb:starter generator:generator dev-answer:answer; do
  archive=${pair%:*}
  folder=${pair#*:}
  curl -fsSL "$base/$archive.zip" -o ".local/$archive.zip"
  unzip -oq ".local/$archive.zip" -d ".local/$folder"
done
cargo build --release --locked --manifest-path .local/generator/generator/Cargo.toml
.local/generator/generator/target/release/generate --seed 1 --events 1000000 --clicks 250000 --output .local/dev
make -j8
make fixtures check-dev
.local/venv/bin/python tests/differential.py .local/shop --seeds 50
```

The official checker compares every field and rejects duplicate output keys. The independent randomized test uses a straightforward Python implementation. It varies worker counts, opaque IDs, timestamp formats, quoting, and cases around the exact session and attribution boundaries. [Rules and official assets](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions)

## Measure runtime

```sh
generate=.local/generator/generator/target/release/generate
"$generate" --seed 734 --events 20000000 --clicks 5000000 --output .local/practice
"$generate" --seed 901 --events 60000000 --clicks 15000000 --output .local/final
python3 tools/benchmark.py .local/shop .local/practice --runs 3 --profile
python3 tools/benchmark.py .local/shop .local/final --runs 3
```

The generator binary already in this workspace was compiled for Linux ARM64. On this Mac, run that binary through the existing local Docker image, as shown below.

```sh
docker run --rm --network none -v "$PWD/.local:/bench" rust:1.89-slim \
  /bench/generator/generator/target/release/generate --seed 734 --events 20000000 \
  --clicks 5000000 --output /bench/practice
```

The benchmark uses an external monotonic clock around each fresh process and a fresh output directory for every run. It includes startup, input reads, Parquet writes, and exit. Input pages can be cached between repetitions. Native macOS and emulated AMD64 runs have different performance characteristics from the hidden Linux scorer.

## Implementation

- Link components use the bytewise smallest member as the person's ID, including aliases that appear only in the link files.
- The ordered reader maps the CSVs and indexes event IDs while a separate task loads identity links and clicks. It uses a bitmap to screen for repeated fingerprints, packs common row locations into four bytes, and revisits possible duplicates in file order for an exact ID comparison. Its mapping budget is 256 MiB per worker, capped at one third of the memory advertised by `MEM_GB` when set. The streaming reader handles larger inputs. Set `SHOP_READER=ordered` or `SHOP_READER=stream` to benchmark either reader directly.
- The streaming reader loads 1 MiB chunks and finishes each owned record across chunk boundaries. It scans 64-byte blocks for commas, quotes and newlines. A two-bit counter identifies event IDs that can be accepted as unique. All possible duplicates go through exact comparison, and the earliest ingested copy wins even when its payload is invalid.
- In the streaming reader, event IDs up to eight bytes store their length and raw bytes directly. Nine-byte ASCII IDs pack losslessly into 63 bits. Longer or non-ASCII nine-byte IDs retain their original bytes in partition-local storage.
- Per-person time ordering determines session starts. Equal-time clicks use bytewise click-ID order. For assists, each campaign gets credit for the union of sessions covered by its clicks, including campaigns that never win a session.
- Parallel column writers emit both tables with UTC dates, integer revenue in cents and a nullable top category.

Public engineering write-ups informed the performance experiments. Fraun describes batched identity lookups and separates warm-cache, cold-cache, process and container timings. Kentucky Fried Evan describes compact event records and campaign interval merging. This implementation treats every ID as an opaque string. [Fraun](https://github.com/mliao0128/fraun-shopping-sessions) · [Kentucky Fried Evan](https://github.com/EvanDamron/shopping-sessions)

Team Uhhh published measurements of cache-sized partitions, buffered reads and exact duplicate filtering. Polaris describes resolving identity links while CSV workers scan input. The local experiments with these techniques are recorded in the benchmark report. [Team Uhhh](https://github.com/matthewf725/shopping-sessions#measurements) · [Polaris](https://github.com/enkyuan/polaris#pipeline)
