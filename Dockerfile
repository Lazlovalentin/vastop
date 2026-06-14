ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:${PYTHON_VERSION}-slim
RUN groupadd --gid 10001 vastop \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin vastop
WORKDIR /app
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl
USER 10001
ENTRYPOINT ["vastai-operator"]
CMD ["run", "--standalone", "--all-namespaces", "--verbose"]
