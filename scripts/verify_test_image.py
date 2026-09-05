#!/usr/bin/env python3
"""
Verify the test image can actually run the suite.

Run during `docker build` / `podman build`. A missing test plugin does not stop
an image building -- it surfaces much later as pytest's "You need to install a
suitable plugin for your async framework", which points at the symptom rather
than the cause. Failing here instead makes the cause obvious.

The likeliest cause, and the reason the message below says so: a pyproject.toml
that has lost its `dependencies` or `[project.optional-dependencies] dev`
section still installs cleanly, and `pip install .[dev]` then installs nothing.
"""

import importlib
import sys

REQUIRED = {
    "pytest": "the test runner",
    "pytest_asyncio": "async test support; without it every async test errors",
    "httpx": "the HTTP client under test",
    "mcp": "the MCP framework; test_server.py cannot even import without it",
}

# asyncio_mode = "auto" in pyproject means async tests carry no marker. If the
# plugin is absent, pytest does not skip them -- it errors on each one, which
# is why this check is worth doing at build time.
PYTEST_CONFIG_KEYS = ("asyncio_mode", "asyncio_default_fixture_loop_scope")


def main() -> int:
    missing = []
    for module, why in REQUIRED.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"  {module:16} {why}")

    if missing:
        print("Test image is incomplete:\n" + "\n".join(missing),
              file=sys.stderr)
        print(
            "\nCheck pyproject.toml still declares both:\n"
            "    [project] dependencies\n"
            "    [project.optional-dependencies] dev\n"
            "Replacing the whole file when only the pytest config was meant to\n"
            "change removes them, and `pip install .[dev]` then succeeds while\n"
            "installing nothing.",
            file=sys.stderr,
        )
        return 1

    try:
        import tomllib
        with open("pyproject.toml", "rb") as handle:
            config = tomllib.load(handle)
        pytest_config = config.get("tool", {}).get("pytest", {}).get(
            "ini_options", {})
        absent = [k for k in PYTEST_CONFIG_KEYS if k not in pytest_config]
        if absent:
            print(f"warning: pyproject pytest config is missing "
                  f"{', '.join(absent)}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 -- advisory only
        print(f"warning: could not read pytest config ({error})",
              file=sys.stderr)

    print("test image OK: " + ", ".join(REQUIRED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
