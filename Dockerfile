ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Install exactly what uv.lock pins (including the CVE overrides in
# pyproject [tool.uv]) into a self-contained venv. pip would ignore the lock
# and the overrides and resolve vastai's vulnerable hard pins.
RUN uv sync --frozen --no-dev --no-editable \
    && /app/.venv/bin/python -c "import vastai_operator"

FROM python:${PYTHON_VERSION}-slim
# The runtime never installs packages: drop pip, setuptools and the bundled
# ensurepip wheels, whose vendored urllib3/msgpack/setuptools carry CVEs.
RUN python -m pip uninstall -y pip setuptools wheel 2>/dev/null; \
    rm -rf /usr/local/lib/python3*/ensurepip /usr/local/bin/pip* \
    && groupadd --gid 10001 vastop \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin vastop
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
WORKDIR /app
USER 10001
ENTRYPOINT ["vastai-operator"]
CMD ["run", "--standalone", "--all-namespaces", "--verbose"]
