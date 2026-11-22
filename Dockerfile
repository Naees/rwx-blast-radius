FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir /sqlglot && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /sqlglot

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN pip install --no-cache-dir duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 python-dateutil==4.0.0 pytz==2026.6 tzdata==2026.6 PyYAML==6.0.3 typing_extensions==4.16.0
COPY --from=source /sqlglot/sqlglot /sqlglot/sqlglot
COPY --from=source /sqlglot/tests /sqlglot/tests
RUN python -m compileall -q /sqlglot
WORKDIR /app
COPY selector.py deps.py impact.py fixtures.py configmap.py observations.py mapdata.py numeric.py registries.py literal_values.py bool_values.py class_checks.py default_args.py sequence_index.py assertion_replay.py query_checks.py verification.py stress.py optimizer_replay.py optimizer_witness.py executor_queries.py fixture_usage.py config_sites.py config_truth.py patchrun.py ./
COPY maps/ ./maps/
RUN python mapdata.py && python -m compileall -q /app
CMD ["python", "selector.py"]
