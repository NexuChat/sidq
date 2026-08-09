# The one environment where Sidq's DataHub aspect contract can be checked.
#
# `acryl-datahub` cannot live in the project venv: it resolves `pydantic` below
# the 2.12 that Sidq's `mcp>=2` client declares, so `make check` runs against a
# contract-checked stand-in and the test that re-derives that contract from the
# real SDK skips with a reason. This image is the environment where it does not
# skip.
#
# The install is deliberately resolver-inconsistent, which is exactly the point:
# it is confined to a container that ships nothing and is thrown away, instead
# of being offered as an extra that would put a contradictory install in an
# operator's path. Nothing here is a runtime dependency of Sidq.
FROM python:3.12-slim

WORKDIR /sidq

# Copied before the source so a source edit does not re-resolve the wheel set.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --editable . \
 && pip install --no-cache-dir acryl-datahub==1.6.0.16 pytest pytest-cov

COPY tests/ ./tests/
COPY data/ ./data/
COPY scripts/ ./scripts/
# Read by guards that check the committed examples against the prose.
COPY examples/ ./examples/
COPY docs/ ./docs/
COPY README.md ARCHITECTURE.md ./

# `-p no:cacheprovider` keeps the container from writing a cache directory into
# a mounted tree. `--no-cov` because coverage is the host suite's gate, not
# this one's: what this image asserts is that the contract still matches.
ENTRYPOINT ["python", "-m", "pytest", "-q", "-rs", "--no-cov", "-p", "no:cacheprovider"]
CMD ["tests/test_assertion_real_sdk.py", "tests/test_receipt.py", \
     "tests/test_mcp_snapshot.py", "tests/test_mcp_server.py"]
