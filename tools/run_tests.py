"""Minimal pytest-compatible runner for environments without pytest.

Supports the subset this suite uses: ``pytest.fixture`` (function scope),
``pytest.raises`` and the ``tmp_path`` fixture. Discovers ``test_*.py``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Raises:
    def __init__(self, expected):
        self.expected = expected
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"{self.expected.__name__} was not raised")
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        return True


def fixture(func=None, **kwargs):
    def wrap(f):
        f._is_fixture = True
        return f

    return wrap(func) if func is not None else wrap


pytest_shim = types.ModuleType("pytest")
pytest_shim.raises = _Raises
pytest_shim.fixture = fixture
sys.modules["pytest"] = pytest_shim


class _CapSys:
    def __init__(self, stdout):
        self._stdout = stdout

    def readouterr(self):
        out = self._stdout.getvalue()
        self._stdout.seek(0)
        self._stdout.truncate(0)
        return types.SimpleNamespace(out=out, err="")


@contextlib.contextmanager
def _capture_stdout():
    import io

    original = sys.stdout
    buffer = io.StringIO()
    sys.stdout = buffer
    try:
        yield _CapSys(buffer)
    finally:
        sys.stdout = original


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    tests_dir = ROOT / "tests"
    conftest = load_module(tests_dir / "conftest.py", "tests.conftest")
    load_module(tests_dir / "support.py", "tests.support")

    fixtures = {"tmp_path": None, "capsys": None}
    for name, obj in inspect.getmembers(conftest, inspect.isfunction):
        if getattr(obj, "_is_fixture", False):
            fixtures[name] = obj

    passed = 0
    failures = []
    for path in sorted(tests_dir.glob("test_*.py")):
        module = load_module(path, f"tests.{path.stem}")
        test_funcs = [
            (name, obj)
            for name, obj in inspect.getmembers(module, inspect.isfunction)
            if name.startswith("test_") and obj.__module__ == module.__name__
        ]
        for name, func in test_funcs:
            try:
                _run(func, fixtures)
                passed += 1
            except Exception:  # noqa: BLE001
                failures.append((f"{path.name}::{name}", traceback.format_exc()))

    print(f"{passed} passed, {len(failures)} failed")
    for name, tb in failures:
        print(f"\nFAILED {name}\n{tb}")
    return 1 if failures else 0


def _run(func, fixtures):
    stack = contextlib.ExitStack()
    cache = {}

    def resolve(param_name):
        if param_name in cache:
            return cache[param_name]
        if param_name == "tmp_path":
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            value = pathlib.Path(tmp)
        elif param_name == "capsys":
            value = stack.enter_context(_capture_stdout())
        elif param_name in fixtures:
            fx = fixtures[param_name]
            kwargs = {name: resolve(name) for name in inspect.signature(fx).parameters}
            value = fx(**kwargs)
        else:
            raise RuntimeError(f"unknown fixture {param_name!r}")
        cache[param_name] = value
        return value

    try:
        kwargs = {name: resolve(name) for name in inspect.signature(func).parameters}
        func(**kwargs)
    finally:
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
