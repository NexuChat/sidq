# Contributing to Sidq

Thank you for helping improve Sidq. Contributions should preserve its central
boundary: evidence collection may fail or abstain, but an unperformed check must
never be reported as clean.

## Development setup

Sidq's tested development runtime is Python 3.12. From the repository root:

```bash
make check
```

The first run creates `.venv` and installs the hash-locked development
dependencies from `requirements-dev.lock`. It therefore needs package-index
access. Later checks reuse that environment. Install `uv` and run `make lock`
only when intentionally updating dependency resolution; commit `uv.lock`,
`requirements.lock`, `requirements-action.lock`, and `requirements-dev.lock`
together.

## Changes

1. Open an issue for behavior or policy changes whose intended outcome is not
   already clear.
2. Add a failing test before the implementation when practical.
3. Keep fixture replay and live DataHub provenance visibly distinct.
4. Never add credentials, private catalog data, or unredistributable corpus rows.
5. Run `make check`. If generated evidence legitimately changes, run the named
   generator and review the complete diff rather than editing artifacts by hand.

Large benchmark intermediates remain local. If the pre-flight regression corpus
changes, regenerate the compact committed artifact with the command in
`data/benchmark/README.md` so clean-clone guards continue to execute.

## Pull requests

Describe the user-visible change, commands actually run, and any verification
that remains manual. Do not present fixture replay as live evidence or a local
result as a merged CI result. By participating, you agree to follow
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
