FROM ubuntu:24.04

# Shared toolchains for all five target languages.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates python3 python3-venv \
       cmake g++ make libgtest-dev \
       openjdk-17-jdk-headless maven dotnet-sdk-8.0 \
    && rm -rf /var/lib/apt/lists/*

# Copy the Rust toolchain into the shared Ubuntu environment.
COPY --from=rust:1-bookworm /usr/local/cargo/ /usr/local/cargo/
COPY --from=rust:1-bookworm /usr/local/rustup/ /usr/local/rustup/
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup
ENV PATH=/opt/test-python/bin:/usr/local/cargo/bin:$PATH

RUN python3 -m venv /opt/test-python \
    && python -m pip install --no-cache-dir "pytest==9.2.0"

# Fail the image build when a required executable cannot run.
RUN python --version \
    && python -m pytest --version \
    && g++ --version \
    && cmake --version \
    && java -version \
    && mvn --version \
    && dotnet --version \
    && rustc --version \
    && cargo --version

WORKDIR /workspace
