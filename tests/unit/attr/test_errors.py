"""Error normalization: ``qualified_name``, ``fingerprint`` and ``error`` (DESIGN §5)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pyrock import attr

HEX16 = re.compile(r"^[0-9a-f]{16}$")
SECRET = "hunter2-very-secret"
SRC_DIR = Path(__file__).resolve().parents[3] / "src"


class CustomError(Exception):
    pass


class Outer:
    class InnerError(Exception):
        pass


class WrapperError(Exception):
    pass


def _catch(fn: Callable[[], object]) -> BaseException:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - the test inspects whatever was raised
        return exc
    raise AssertionError("expected an exception")


def _raise_value_error(message: str = "bad input") -> None:
    raise ValueError(message)


def _raise_value_error_elsewhere(message: str = "bad input") -> None:
    raise ValueError(message)


def _raise_custom() -> None:
    raise CustomError("custom")


_FP_SOURCE = """
def inner_{tag}(message):
    raise {exc}(message)

def outer_{tag}(message):
    inner_{tag}(message)
"""


def _exec_raiser(
    *, tag: str = "a", exc: str = "ValueError", offset: int = 0, module: str = "fpmod"
) -> Callable[[str], None]:
    """Define ``outer_<tag>`` in a synthetic module, shifted down by ``offset`` lines."""
    ns: dict[str, Any] = {"__name__": module}
    code = compile("\n" * offset + _FP_SOURCE.format(tag=tag, exc=exc), f"<{module}>", "exec")
    exec(code, ns)
    fn: Callable[[str], None] = ns[f"outer_{tag}"]
    return fn


def _fp_of(raiser: Callable[[str], None], message: str = "m", **kw: Any) -> str:
    return attr.fingerprint(_catch(lambda: raiser(message)), **kw)


# ----------------------------------------------------------- qualified_name


def test_qualified_name_builtin() -> None:
    assert attr.qualified_name(ValueError) == "builtins.ValueError"
    assert attr.qualified_name(KeyError) == "builtins.KeyError"


def test_qualified_name_custom_module_class() -> None:
    assert attr.qualified_name(CustomError) == f"{__name__}.CustomError"


def test_qualified_name_nested_class_uses_qualname() -> None:
    assert attr.qualified_name(Outer.InnerError) == f"{__name__}.Outer.InnerError"


def test_qualified_name_stdlib_module() -> None:
    import json

    assert attr.qualified_name(json.JSONDecodeError) == "json.decoder.JSONDecodeError"


# -------------------------------------------------------------- fingerprint


def test_fingerprint_is_16_lowercase_hex() -> None:
    assert HEX16.match(attr.fingerprint(_catch(_raise_value_error)))


def test_fingerprint_without_traceback() -> None:
    first = attr.fingerprint(ValueError("never raised"))
    assert HEX16.match(first)
    assert attr.fingerprint(ValueError("other text")) == first
    assert attr.fingerprint(KeyError("k")) != first


def test_fingerprint_is_stable_at_one_call_site() -> None:
    fps = {attr.fingerprint(_catch(_raise_value_error)) for _ in range(20)}
    assert len(fps) == 1


def test_fingerprint_ignores_the_message() -> None:
    a = attr.fingerprint(_catch(lambda: _raise_value_error("one")))
    b = attr.fingerprint(_catch(lambda: _raise_value_error(SECRET)))
    assert a == b


def test_fingerprint_differs_per_call_site() -> None:
    a = attr.fingerprint(_catch(_raise_value_error))
    b = attr.fingerprint(_catch(_raise_value_error_elsewhere))
    assert a != b


def test_fingerprint_differs_per_type() -> None:
    a = _fp_of(_exec_raiser(tag="t", exc="ValueError"))
    b = _fp_of(_exec_raiser(tag="t", exc="KeyError"))
    assert a != b


def test_fingerprint_differs_per_module() -> None:
    a = _fp_of(_exec_raiser(module="mod_one"))
    b = _fp_of(_exec_raiser(module="mod_two"))
    assert a != b


@pytest.mark.parametrize("offset", [1, 7, 100, 2500])
def test_fingerprint_excludes_line_numbers(offset: int) -> None:
    """The same functions shifted down by unrelated lines keep their fingerprint."""
    base = _fp_of(_exec_raiser(offset=0))
    assert _fp_of(_exec_raiser(offset=offset)) == base


def test_fingerprint_frames_limits_the_frames_considered() -> None:
    """Two stacks that share only their first and last frame."""
    leaf = _exec_raiser(tag="leaf")

    def via_a() -> None:
        leaf("m")

    def via_b() -> None:
        leaf("m")

    exc_a = _catch(lambda: via_a())
    exc_b = _catch(lambda: via_b())
    assert attr.fingerprint(exc_a, frames=8) != attr.fingerprint(exc_b, frames=8)
    assert HEX16.match(attr.fingerprint(exc_a, frames=1))


def test_fingerprint_is_stable_across_processes_and_hash_seeds() -> None:
    script = (
        "from pyrock import attr\n"
        "def boom():\n"
        "    raise ValueError('x')\n"
        "try:\n"
        "    boom()\n"
        "except ValueError as e:\n"
        "    print(attr.fingerprint(e))\n"
    )
    outputs = []
    for seed in ("0", "1", "12345"):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONPATH": os.pathsep.join([str(SRC_DIR), os.environ.get("PYTHONPATH", "")]),
        }
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        if "NotImplementedError" in proc.stderr:
            raise NotImplementedError(proc.stderr.strip().splitlines()[-1])
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout.strip())
    assert HEX16.match(outputs[0])
    assert len(set(outputs)) == 1


# -------------------------------------------------------------------- error


def test_error_basic_keys_and_types() -> None:
    exc = _catch(lambda: _raise_value_error(SECRET))
    out = attr.error(exc)
    assert out[attr.ERROR_KEY] is True
    assert out[attr.ERROR_TYPE] == "builtins.ValueError"
    fp = out[attr.ERROR_FINGERPRINT]
    assert isinstance(fp, str)
    assert HEX16.match(fp)
    assert fp == attr.fingerprint(exc)


def test_error_omits_message_by_default() -> None:
    out = attr.error(_catch(lambda: _raise_value_error(SECRET)))
    assert attr.ERROR_MESSAGE not in out
    assert not any(isinstance(v, str) and SECRET in v for v in out.values())
    assert not any(isinstance(v, list) and any(SECRET in str(i) for i in v) for v in out.values())


def test_error_message_with_allow_details() -> None:
    out = attr.error(_catch(lambda: _raise_value_error("visible detail")), allow_details=True)
    assert out[attr.ERROR_MESSAGE] == "visible detail"


def test_error_works_for_unraised_exception() -> None:
    out = attr.error(CustomError("x"))
    assert out[attr.ERROR_KEY] is True
    assert out[attr.ERROR_TYPE] == f"{__name__}.CustomError"
    assert HEX16.match(str(out[attr.ERROR_FINGERPRINT]))


def test_error_keys_are_all_in_error_namespace() -> None:
    out = attr.error(_catch(_raise_custom), stack=True, allow_details=True, allow_source_paths=True)
    for key in out:
        assert key == "error" or key.startswith("error."), key


# -------------------------------------------------------------------- chain


def _chain(exc: BaseException) -> list[str]:
    value = attr.error(exc).get(attr.ERROR_CHAIN, [])
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return list(value)


def test_unwrapped_error_has_no_other_chain_entries() -> None:
    exc = _catch(_raise_value_error)
    assert set(_chain(exc)) <= {"builtins.ValueError"}


def test_chain_follows_explicit_cause() -> None:
    def raiser() -> None:
        try:
            raise KeyError("k")
        except KeyError as inner:
            raise WrapperError("wrapped") from inner

    assert "builtins.KeyError" in _chain(_catch(raiser))


def test_chain_follows_implicit_context() -> None:
    def raiser() -> None:
        try:
            raise KeyError("k")
        except KeyError:
            raise WrapperError("during handling")  # noqa: B904 - testing __context__

    assert "builtins.KeyError" in _chain(_catch(raiser))


def test_chain_honours_suppress_context() -> None:
    def raiser() -> None:
        try:
            raise KeyError("k")
        except KeyError:
            raise WrapperError("clean") from None

    assert "builtins.KeyError" not in _chain(_catch(raiser))


def test_explicit_cause_wins_over_context() -> None:
    def raiser() -> None:
        try:
            raise KeyError("k")
        except KeyError:
            raise WrapperError("w") from TypeError("explicit")

    chain = _chain(_catch(raiser))
    assert "builtins.TypeError" in chain
    assert "builtins.KeyError" not in chain


def test_chain_is_ordered_outer_to_inner() -> None:
    def raiser() -> None:
        try:
            try:
                raise KeyError("innermost")
            except KeyError as e:
                raise TypeError("middle") from e
        except TypeError as e:
            raise WrapperError("outer") from e

    chain = _chain(_catch(raiser))
    assert chain.index("builtins.TypeError") < chain.index("builtins.KeyError")


def test_chain_cycles_terminate() -> None:
    a = ValueError("a")
    b = KeyError("b")
    a.__cause__ = b
    b.__cause__ = a
    chain = _chain(a)
    assert "builtins.KeyError" in chain
    assert len(chain) <= 3


def test_context_cycle_terminates() -> None:
    a = ValueError("a")
    a.__context__ = a
    assert len(_chain(a)) <= 2


def test_long_chain_terminates_property() -> None:
    head: BaseException = ValueError("0")
    current = head
    for i in range(1, 500):
        nxt = KeyError(str(i)) if i % 2 else ValueError(str(i))
        current.__cause__ = nxt
        current = nxt
    chain = _chain(head)
    assert len(chain) <= 500


# ------------------------------------------------------------------- groups


def test_exception_group_summary() -> None:
    group = ExceptionGroup("several", [ValueError("a"), KeyError("b")])
    out = attr.error(group)
    assert out[attr.ERROR_TYPE] == "builtins.ExceptionGroup"
    count = out[attr.ERROR_GROUP_COUNT]
    assert type(count) is int
    assert count == 2
    types = out[attr.ERROR_TYPES]
    assert isinstance(types, list)
    assert sorted(types) == ["builtins.KeyError", "builtins.ValueError"]


def test_nested_exception_group_reports_leaves() -> None:
    group = ExceptionGroup(
        "outer",
        [ValueError("a"), ExceptionGroup("inner", [KeyError("b"), CustomError("c")])],
    )
    out = attr.error(group)
    assert out[attr.ERROR_GROUP_COUNT] == 3
    types = out[attr.ERROR_TYPES]
    assert isinstance(types, list)
    assert sorted(types) == sorted(
        ["builtins.ValueError", "builtins.KeyError", f"{__name__}.CustomError"]
    )
    assert "builtins.ExceptionGroup" not in types


def test_raised_exception_group_summary() -> None:
    def raiser() -> None:
        raise ExceptionGroup("g", [TypeError("t")])

    out = attr.error(_catch(raiser))
    assert out[attr.ERROR_GROUP_COUNT] == 1
    assert out[attr.ERROR_TYPES] == ["builtins.TypeError"]


def test_plain_exception_has_no_group_keys() -> None:
    out = attr.error(ValueError("x"))
    assert attr.ERROR_GROUP_COUNT not in out
    assert attr.ERROR_TYPES not in out


# -------------------------------------------------------------------- stack


def _stacked() -> BaseException:
    def deep_raiser_fn() -> None:
        raise CustomError("stacked")

    return _catch(deep_raiser_fn)


def test_no_stack_unless_requested() -> None:
    out = attr.error(_stacked(), allow_source_paths=True, allow_details=True)
    assert attr.ERROR_STACK not in out


def test_full_stack_with_source_paths() -> None:
    out = attr.error(_stacked(), stack=True, allow_source_paths=True)
    stack = out[attr.ERROR_STACK]
    assert isinstance(stack, str)
    assert __file__ in stack
    assert "deep_raiser_fn" in stack


def test_stack_without_source_paths_is_trimmed() -> None:
    out = attr.error(_stacked(), stack=True)
    stack = out.get(attr.ERROR_STACK)
    if stack is None:
        return
    assert isinstance(stack, str)
    assert __file__ not in stack
    assert "/" not in stack
    assert "\\" not in stack
    assert "deep_raiser_fn" in stack


def test_stack_never_contains_message_without_details() -> None:
    def raiser() -> None:
        raise ValueError(SECRET)

    out = attr.error(_catch(raiser), stack=True, allow_source_paths=True)
    assert SECRET not in str(out.get(attr.ERROR_STACK, ""))
