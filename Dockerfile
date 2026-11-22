FROM python:3.13-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ make && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --no-deps pyarrow==21.0.0
WORKDIR /src
COPY *.cpp *.hpp launcher.c Makefile ./
RUN mkdir -p /opt/runtime/lib && \
    make -j4 BUILD=/opt/build/portable && cp /opt/build/portable/shop /opt/runtime/shop-portable && \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        make -j4 BUILD=/opt/build/avx2 CXXFLAGS="-O3 -DNDEBUG -mavx2 -mpopcnt" && \
        cp /opt/build/avx2/shop /opt/runtime/shop-avx2; \
    fi && \
    cc -O2 launcher.c -o /opt/runtime/shop && \
    cp /usr/local/lib/python3.13/site-packages/pyarrow/libarrow.so.2100 \
       /usr/local/lib/python3.13/site-packages/pyarrow/libparquet.so.2100 /opt/runtime/lib/
FROM debian:bookworm-slim
COPY --from=build /opt/runtime/ /opt/shop/
COPY --from=build /usr/local/lib/python3.13/site-packages/pyarrow-21.0.0.dist-info/licenses/ /opt/shop/licenses/
ENV LD_LIBRARY_PATH=/opt/shop/lib
USER 1000
ENTRYPOINT ["/opt/shop/shop"]
