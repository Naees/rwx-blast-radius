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

For edits to an `isinstance` check, the selector compares the old and new class arguments against the inheritance chains observed at that call. When a Boolean class flag changes, it can also rule out tests where the surrounding condition would have taken the same branch for either flag value. For an eligible change to a function's default argument it checks whether the body uses the parameter and which callers were measured. A comment in a compound patch can still select tests that read that Python file as data.

An integer index edit can leave a test unaffected when both indices refer to the same object in every observed list or tuple. A changed default can likewise leave a caller unaffected if it supplied an explicit argument. For expensive TPC tests, the selector also checks observed function signatures and object types before replaying optimizer stages.

For a single-file edit, the selector can recognize equivalent emptiness checks on local containers created by literals or comprehensions. It also recognizes an in-place reversal of an exact list expressed through slice assignment. These checks inspect other assignments to the local and preserve tests that read the source itself.

The Boolean profiler can read a declared Python slot when it verifies the receiver class and the slot descriptor. For a direct call changed from positional to keyword arguments, the selector checks the callee, parameter binding and evaluation order. It retains tests that may replace the callable, inspect its code or run it in another process.

The selector can run candidate tests in a temporary checkout containing the patch. For a single-file source edit it uses baseline coverage to narrow independent SQL fixture rows and dialect assertions. For selected expensive tests it can replay optimizer stages, compare the resulting trees with the baseline, or run individual executor queries against DuckDB. For edits to fixture data it distinguishes the input SQL, expected SQL and metadata a test consumed. Probe failures and timeouts leave unverified tests in the schedule.

The prediction is empirical. New control flow, dynamic imports and differences between profiling and scoring environments can affect recall. The organizer's sample answers are used only by the external scorer.

## Rebuild the maps

Use a clean checkout of the pinned revision and a CPython 3.13 virtual environment containing the test dependencies from the organizer's `requirements.lock`. Activate the environment so child interpreters resolve the same Python. From this repository, set `BASE` to the checkout and `TESTS` to the organizer's `tests.json`, then run the generators sequentially.

```sh
mkdir -p .work
export PYTHONHASHSEED=0 SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1
python generate_map.py "$BASE" "$TESTS" maps/deps.json.gz
python observe_base.py "$BASE" "$TESTS" maps/observations.json.gz
python generate_config.py "$BASE" "$TESTS" maps/config.json.gz
python generate_arguments.py "$BASE" "$TESTS" maps/arguments.json.gz
python generate_constants.py "$BASE" maps/constants.json.gz
python generate_scopes.py "$BASE" maps/scopes.json.gz
python generate_imports.py "$BASE" "$TESTS"
python generate_initializers.py "$BASE" "$TESTS" maps/initializers.json.gz
python fixtures.py "$BASE"
python generate_assertions.py "$BASE" "$TESTS" maps/assertions.json.gz
python generate_queries.py "$BASE" "$TESTS" maps/queries.json.gz
python generate_fixture_usage.py "$BASE" "$TESTS" maps/fixture_usage.json.gz
python generate_config_sites.py "$BASE" "$TESTS" maps/config_sites.json.gz
python generate_stress.py "$BASE" maps/stress.json.gz
python generate_numeric.py "$BASE" "$TESTS" maps/numeric.json.gz
python generate_literal_values.py "$BASE" "$TESTS" maps/literal_values.json.gz
python generate_bool_values.py "$BASE" "$TESTS" maps/bool_values.json.gz
python generate_call_binding.py "$BASE" maps/call_binding.json.gz
python generate_config_truth.py "$BASE" "$TESTS" maps/config_truth.json.gz
python generate_class_checks.py "$BASE" "$TESTS" maps/class_checks.json.gz
python default_args.py "$BASE" maps/default_args.json.gz
python generate_default_values.py "$BASE" "$TESTS" maps/default_values.json.gz
python generate_sequence_index.py "$BASE" "$TESTS" maps/sequence_index.json.gz
python generate_registries.py "$BASE" "$TESTS" .work/registry-all.json.gz
python pack_registries.py "$BASE" maps/registries.json.gz .work/registry-all.json.gz
python optimizer_replay_build.py "$BASE"
python generate_optimizer_witness.py "$BASE" maps/optimizer_witness.json.gz
python executor_queries_build.py "$BASE"
```

The submitted image uses the committed maps. Docker fetches the pinned SQLGlot source, installs dependencies for the runtime probes and compiles the maps into a local cache during the build.

[Competition rules and starter kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius) · [Testmon's explanation of dependency-based test selection](https://www.testmon.org/blog/determining-affected-tests/)
