# Local Shopping Sessions benchmarks

**Single-pass events** had the lowest median on the 60M-event test. All six final-size container outputs matched a separately computed reference. At 04:23 EST on November 22, 2026, the official practice leaderboard showed **LSU_i_Bosna_Najbolja at 0.4293 seconds**. This project was tested locally. [Official leaderboard](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions)

## Container timings

Times are seconds from Docker's recorded start to exit. Each measurement used a fresh Linux AMD64 container on an Apple Silicon host running OrbStack. The input lived in a Docker volume and was read before every trial. The harness rotated the image order between rounds and verified the results after timing.

| Implementation | 20M practice runs | Median | 60M final-size runs | Median |
| --- | ---: | ---: | ---: | ---: |
| First C++ build | 8.564, 8.591, 10.833 | 8.591 | — | — |
| Ordered duplicate reads | 4.884, 6.972, 6.226 | 6.226 | 78.960, 113.588, 117.715 | 113.588 |
| Single-pass events | 7.277, 9.051, 5.246 | 7.277 | 51.631, 67.391, 89.369 | 67.391 |

The fastest practice implementation reduced the initial C++ median by 27.5%. At final size, single-pass events reduced median runtime by 40.7% relative to ordered duplicate reads. Every measured final-size container finished inside 180 seconds.

The local VM provided eight pinned logical CPUs and a 7 GiB memory limit with swap disabled for the container. Runs used UID 1000, a read-only root, a 64 MiB temporary filesystem and disabled networking. Both optimized images are approximately 136 MB. The organizer runs hidden inputs on Linux AMD64 hardware with 24 GB of memory. The original C++ image is `cc6b1dc54938`, the ordered reader is commit `e4b0f95` and the single-pass reader is commit `10ab206`. [Scoring environment](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions)

## Output verification

| Input | User-day rows | Campaign-day rows | Check |
| --- | ---: | ---: | --- |
| Official dev, seed 1, 1M events and 250K clicks | 118,107 | 27,449 | Official checker, both tables match and no duplicate keys |
| Practice, seed 734, 20M events and 5M clicks | 2,261,065 | 28,014 | All nine timed outputs match the independent SQL reference |
| Final size, seed 901, 60M events and 15M clicks | 6,640,626 | 28,014 | All six timed outputs match the independent SQL reference |

The 36 organizer fixtures cover parsing, linked IDs, sessions and attribution. Additional randomized tests use a separate Python implementation, including exact boundary times, opaque identifiers, a wide manifest window, cross-chunk records and a missing terminal newline.

The dev and practice inputs came from the unmodified v5 generator. The local Docker VM has about 8 GiB, so final-size generation spooled the generator's intermediate data to disk and used its original shuffling logic. This version reproduced the unmodified generator's seed-1 dev files byte for byte. The independent reference used a separate identity implementation and DuckDB 2.0.1. The comparison checks every column and preserves duplicate multiplicity. [Official data generator](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions) · [Reference checker at its tested revision](https://github.com/mliao0128/fraun-shopping-sessions/blob/b78bed79ec3bf93e38a74b3f077d6ba125658c84/tools/check_generated.py)

## Reading the native timings

On the Mac's native ARM64 executable, the single-pass reader used 1,847 MiB peak RSS on the practice dataset, compared with 3,355 MiB for the ordered reader. Their medians in that paired test were 3.625 s and 3.854 s. A single native final-size trial of the single-pass prototype finished in 22.474 s. Cache residency and system memory pressure varied across experiments. Use the interleaved container series above to compare the Linux images.

## Repeat the comparison

In this local checkout, the generated data, reference databases and raw JSON reports are under `.local/`. From a fresh checkout, follow the dataset setup in [README.md](README.md). The optional `--oracle` argument uses DuckDB to verify the output outside the timing interval.

On a fresh checkout, build the optional final-size reference in a new directory before the container comparison. This step is separate from timing.

```sh
.local/venv/bin/python -m pip install duckdb==2.0.1
curl -fsSL https://raw.githubusercontent.com/mliao0128/fraun-shopping-sessions/b78bed79ec3bf93e38a74b3f077d6ba125658c84/tools/check_generated.py -o .local/check_generated.py
.local/shop .local/final .local/final-candidate
.local/venv/bin/python .local/check_generated.py .local/final .local/final-candidate .local/oracle-final --threads 4 --memory-gib 3
```

Build the two image revisions and run the comparison.

```sh
git archive e4b0f95 | docker build --platform linux/amd64 -t shopping-sessions-local:ordered -
git archive 10ab206 | docker build --platform linux/amd64 -t shopping-sessions-local:stream -
.local/venv/bin/python tools/benchmark_containers.py \
  --entry ordered=shopping-sessions-local:ordered --entry stream=shopping-sessions-local:stream \
  --data .local/final --oracle .local/oracle-final/oracle.duckdb --runs 3 --memory-gib 7
```

The practice report is `.local/docker-benchmark-d8pn9mq3/results.json`. The final-size report is `.local/docker-benchmark-fnw00boc/results.json`.
