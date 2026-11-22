FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir /sqlglot && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /sqlglot

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
COPY --from=source /sqlglot/sqlglot /sqlglot/sqlglot
COPY --from=source /sqlglot/tests /sqlglot/tests
WORKDIR /app
COPY selector.py deps.py impact.py fixtures.py configmap.py observations.py mapdata.py numeric.py registries.py ./
COPY maps/ ./maps/
RUN python mapdata.py && python -m compileall -q /app
CMD ["python", "selector.py"]
