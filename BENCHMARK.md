# Shopping Sessions benchmarks

In the local Linux AMD64 VM, the adaptive image at `65ee24e` recorded median runtimes of **5.936 seconds on 20M events** and **83.043 seconds on 60M events**. Compared with the previous default at `10ab206`, its median was 31.1% lower at practice size and 20.3% lower at final size. The adaptive image finished ahead of the previous default in all seven paired rounds. All 17 runs in these two comparisons matched both reference tables.

## Official practice submission

The organizer recorded **2.2478 seconds** for `65ee24e` on the hidden practice dataset. At 05:38 EST on November 22, 2026, the leaderboard lead was 0.4218 seconds. [Submitted commit](https://github.com/Naees/rwx-blast-radius/commit/65ee24ef340c216eed66a79a3a6044a530cfcd02) · [Official track and leaderboard](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions)

## Selected image in the local VM

Times are seconds from Docker’s recorded container start to exit. Each round used fresh Linux AMD64 containers on Apple Silicon with OrbStack. Images alternated or rotated within a series. The harness read the input volume before every trial and checked output after timing.

| Events | Image and reader | Runs in round order (s) | Median (s) | Min–max (s) |
| --- | --- | --- | ---: | ---: |
| 20M | Previous streaming reader, `10ab206` | 7.014, 9.335, 8.615 | 8.615 | 7.014–9.335 |
| 20M | Standalone ordered reader, `e4b0f95` | 5.501, 4.499, 8.895 | 5.501 | 4.499–8.895 |
| 20M | Adaptive, ordered reads, `65ee24e` | 5.001, 5.936, 7.521 | 5.936 | 5.001–7.521 |
| 60M | Previous streaming reader, `10ab206` | 111.782, 119.207, 81.471, 96.634 | 104.208 | 81.471–119.207 |
| 60M | Adaptive, streaming reads, `65ee24e` | 98.975, 62.220, 69.804, 96.283 | 83.043 | 62.220–98.975 |

With eight workers and a 7 GiB memory limit, the adaptive image used a 2 GiB event-CSV threshold. Stage logs confirmed ordered reads for the 1.61 GiB practice input and streaming reads for the 4.84 GiB final-size input. The threshold was tuned on those two workloads. Compare controls within the same series because VM memory pressure and filesystem cache residency varied across tests.

The containers used eight pinned guest CPUs, UID 1000, a read-only root, a 64 MiB temporary filesystem, disabled networking and a 7 GiB memory limit with swap disabled. The organizer scores hidden datasets on native Linux AMD64 with eight vCPUs and 24 GB of memory. [Official track and scoring rules](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions)

## Reader experiments

Each cell gives the median wall time in seconds, with the observed minimum and maximum in parentheses. The control in every row is the previous default at `10ab206`.

| Experiment | Events | Runs per image | Control | Alternatives |
| --- | --- | ---: | ---: | --- |
| Compact records and parallel identity resolution | 20M | 3 | 10.624 (10.132–17.161) | Compact events 15.024 (11.890–15.395), combined changes 14.944 (14.237–15.116) |
| Compact records and early memory release | 60M | 3 | 47.537 (36.359–48.774) | Combined changes 47.650 (37.898–66.031), early release and radix sorting 63.185 (41.689–66.603) |
| Sparse duplicate filter and block parser | 20M | 4 | 5.493 (5.237–5.975) | Sparse filter 5.637 (4.990–5.872), filter plus block parser 5.271 (5.012–5.817), standalone ordered reader 4.714 (4.018–6.339) |
| Sparse duplicate filter and block parser | 60M | 3 | 18.486 (16.852–45.032) | Sparse filter 27.130 (15.503–28.048), filter plus block parser 16.468 (14.151–23.434) |

Team Uhhh published measurements on cache-sized duplicate partitions and checked buffered reads. Polaris describes overlapping identity work with input scanning. SophieZ596 documented parser time separately from aggregation on a CPU-constrained machine. Those reports prompted independent experiments in this project. [Team Uhhh’s measurements](https://github.com/matthewf725/shopping-sessions#measurements) · [Polaris pipeline](https://github.com/enkyuan/polaris#pipeline) · [SophieZ596’s measured results](https://github.com/SophieZ596/shopping-sessions#measured-results)

The compact alternative is at `712b3aa`, and the version with early memory release is at `652a420`. A separate native overlap experiment increased the 20M median from 5.511 to 6.703 seconds. The sparse-filter revision is `d3055b2`. At `4a1a583` the streaming reader additionally scans CSV separators, quotes and newlines together in 64-byte blocks. A saturated bitmap counter sends every possible duplicate to full ID comparison, including invalid rows that may suppress a later valid copy.

## Earlier container measurements

These two series compared the initial C++ image (`cc6b1dc54938`), the standalone ordered reader (`e4b0f95`) and the original single-pass reader (`10ab206`). Both tables matched the reference in all 15 runs.

| Implementation | 20M practice runs | Median | 60M final-size runs | Median |
| --- | ---: | ---: | ---: | ---: |
| First C++ build | 8.564, 8.591, 10.833 | 8.591 | — | — |
| Ordered duplicate reads | 4.884, 6.972, 6.226 | 6.226 | 78.960, 113.588, 117.715 | 113.588 |
| Single-pass events | 7.277, 9.051, 5.246 | 7.277 | 51.631, 67.391, 89.369 | 67.391 |

## Correctness

The independent DuckDB comparison checked all columns and preserved duplicate multiplicity. Across the six selected-image and reader-experiment series, all 60 timed runs matched their references. Every run exited successfully, with zero container OOM kills.

| Generated dataset | User-day rows | Campaign-day rows | Verification |
| --- | ---: | ---: | --- |
| Development, seed 1, 1M events and 250K clicks | 118,107 | 27,449 | Both tables matched the published answer, with no duplicate keys |
| Practice, seed 734, 20M events and 5M clicks | 2,261,065 | 28,014 | All 34 timed runs across three series matched both reference tables |
| Final size, seed 901, 60M events and 15M clicks | 6,640,626 | 28,014 | All 26 timed runs across three series matched both reference tables |

Both adaptive reader paths passed the 36 organizer fixtures and 96 randomized end-to-end checks per reader. These checks cover opaque identifiers, transitive identity, wide time windows, exact timestamp boundaries, session credit and assists.

The dev and practice inputs came from the unmodified v5 generator. To generate the final-size input within the VM’s approximately 8 GiB of memory, the generator spooled intermediate data to disk and retained its original shuffling logic. This version reproduced the unmodified generator's seed-1 dev files byte for byte. The independent reference used a separate identity implementation and DuckDB 2.0.1. [Official data generator](https://www.joinbuilders.org/buildathon/tracks/shopping-sessions) · [Reference checker at its tested revision](https://github.com/mliao0128/fraun-shopping-sessions/blob/b78bed79ec3bf93e38a74b3f077d6ba125658c84/tools/check_generated.py)

## Native ARM64 measurements

On the Mac's native ARM64 executable, the single-pass reader used 1,847 MiB peak RSS on the practice dataset, compared with 3,355 MiB for the ordered reader. Their medians in that paired test were 3.625 s and 3.854 s. A single native final-size trial of the single-pass prototype finished in 22.474 s. Cache residency and system memory pressure varied across experiments. Use the interleaved container series above to compare the Linux images.

## Reproduce the comparison

Generate the practice and final inputs using the [project setup instructions](README.md). To create the independent reference databases once, run the pinned checker with a new reference directory for each dataset.

```sh
.local/venv/bin/python -m pip install duckdb==2.0.1
curl -fsSL https://raw.githubusercontent.com/mliao0128/fraun-shopping-sessions/b78bed79ec3bf93e38a74b3f077d6ba125658c84/tools/check_generated.py -o .local/check_generated.py
for scale in practice final; do
  .local/shop ".local/$scale" ".local/$scale-candidate"
  .local/venv/bin/python .local/check_generated.py ".local/$scale" ".local/$scale-candidate" ".local/oracle-$scale" --threads 4 --memory-gib 3
done
```

Build the measured revisions and run the two selected-image comparisons.

```sh
git archive 10ab206 | docker build --platform linux/amd64 -t shopping-sessions-local:10ab206 -
git archive e4b0f95 | docker build --platform linux/amd64 -t shopping-sessions-local:e4b0f95 -
git archive 65ee24e | docker build --platform linux/amd64 -t shopping-sessions-local:65ee24e -
.local/venv/bin/python tools/benchmark_containers.py \
  --entry previous=shopping-sessions-local:10ab206 --entry ordered=shopping-sessions-local:e4b0f95 \
  --entry adaptive=shopping-sessions-local:65ee24e --data .local/practice \
  --oracle .local/oracle-practice/oracle.duckdb --runs 3 --memory-gib 7 --profile
.local/venv/bin/python tools/benchmark_containers.py \
  --entry previous=shopping-sessions-local:10ab206 --entry adaptive=shopping-sessions-local:65ee24e \
  --data .local/final --oracle .local/oracle-final/oracle.duckdb --runs 4 --memory-gib 7 --profile
```

The harness prints the path to each JSON report. In these measurements the practice and final-size report directories were `docker-benchmark-haldymlw` and `docker-benchmark-k5wneirz`. The four experiment report directories, in table order, ended in `a3kp36_h`, `6xn_qa4j`, `e3l_ymfi` and `phgivmw7`. The two earlier container reports ended in `d8pn9mq3` and `fnw00boc`. Each report records individual times, image IDs, exit codes and verification results.
