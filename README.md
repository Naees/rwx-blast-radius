# RWX Blast Radius

This selector predicts which SQLGlot tests to run after a patch and distributes them across up to eight workers. The image reads the organizer's test inventory and patches from `/data` and emits one JSON line per patch.

## Run the sample

```sh
make sample
python3 benchmark.py
```

The Makefile downloads the official sample into `.work/`, builds a Linux amd64 image, runs it with the competition's resource limits and scores its output. The second command runs the image three more times, measures container CPU through cgroup v2 and reports the median total. Local CPU measurements depend on the machine and architecture.

## How selection works

The dependency maps come from the unmodified SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The selector applies each patch in memory and compares its Python syntax with the original. A scope index reduces the amount of source it needs to parse. Test coverage links changed executable lines to test IDs. Baseline observations of comparisons and short-circuit conditions can rule out tests when the recorded values produce the same result under the edit.

Separate maps track class attributes, registry lookups, fixture reads, Python files read as data, and code reached during imports. The profiler also follows serialized arguments and results at process-pool boundaries. The selector uses the measured test durations to compare one through eight worker schedules, including the two-second cost for each worker.

The prediction is empirical. New control flow, dynamic imports and differences between profiling and scoring environments can affect recall. The organizer's sample answers are used only by the external scorer.

## Rebuild the maps

Use a clean checkout of the pinned revision and a CPython 3.13 virtual environment containing the test dependencies from the organizer's `requirements.lock`. Activate the environment so child interpreters resolve the same Python. From this repository, set `BASE` to the checkout and `TESTS` to the organizer's `tests.json`, then run the generators sequentially.

```sh
export SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1
python generate_map.py "$BASE" "$TESTS" maps/deps.json.gz
python observe_base.py "$BASE" "$TESTS" maps/observations.json.gz
python generate_config.py "$BASE" "$TESTS" maps/config.json.gz
python generate_arguments.py "$BASE" "$TESTS" maps/arguments.json.gz
python generate_constants.py "$BASE" maps/constants.json.gz
python generate_scopes.py "$BASE" maps/scopes.json.gz
python generate_imports.py "$BASE" "$TESTS"
python generate_initializers.py "$BASE" "$TESTS" maps/initializers.json.gz
python fixtures.py "$BASE"
```

The submitted image uses the committed maps. Docker fetches the pinned SQLGlot source and compiles the maps into a local cache during the build. Prediction uses the Python standard library.

[Competition rules and starter kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius) · [Testmon's explanation of dependency-based test selection](https://www.testmon.org/blog/determining-affected-tests/)
