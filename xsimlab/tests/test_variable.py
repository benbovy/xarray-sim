import pytest
import attr

import xsimlab as xs
from xsimlab.tests.fixture_process import SomeProcess, AnotherProcess, ExampleProcess
from xsimlab.variable import _as_dim_tuple, _as_group_tuple


@pytest.mark.parametrize(
    "dims,expected",
    [
        ((), ((),)),
        ([], ((),)),
        ("", ((),)),
        (("x"), (("x",),)),
        (["x"], (("x",),)),
        ("x", (("x",),)),
        (("x", "y"), (("x", "y"),)),
        ([(), "x", ("x", "y")], ((), ("x",), ("x", "y"))),
    ],
)
def test_as_dim_tuple(dims, expected):
    assert _as_dim_tuple(dims) == expected


def test_as_dim_tuple_invalid():
    invalid_dims = ["x", "y", ("x", "y"), ("y", "x")]

    with pytest.raises(ValueError) as excinfo:
        _as_dim_tuple(invalid_dims)
    assert "following combinations" in str(excinfo.value)
    assert "('x',), ('y',) and ('x', 'y'), ('y', 'x')" in str(excinfo.value)


@pytest.mark.parametrize(
    "groups,group,expected",
    [
        (None, None, ()),
        ("group1", None, ("group1",)),
        (["group1", "group2"], None, ("group1", "group2")),
        ("group1", "group2", ("group1", "group2")),
        ("group1", "group1", ("group1",)),
    ],
)
def test_as_group_tuple(groups, group, expected):
    if group is not None:
        with pytest.warns(FutureWarning):
            actual = _as_group_tuple(groups, group)

    else:
        actual = _as_group_tuple(groups, group)

    assert actual == expected


def test_variable():
    # test constructor
    @attr.attrs
    class Foo:
        some_var = xs.variable()
        another_var = xs.variable(intent="out")

    assert Foo(some_var=2).some_var == 2

    with pytest.raises(TypeError):
        # intent='out' not in constructor
        Foo(another_var=2)


def test_index():
    with pytest.raises(ValueError, match=r".*not accept scalar values.*"):
        xs.index(())

    # test constructor
    @attr.attrs
    class Foo:
        var = xs.index(dims="x")

    with pytest.raises(TypeError):
        # index variable not in contructor (intent='out')
        Foo(var=2)


def test_on_demand():
    # test constructor
    @attr.attrs
    class Foo:
        var = xs.on_demand()

    with pytest.raises(TypeError):
        # on_demand variable not in contructor (intent='out')
        Foo(var=2)


def test_any_object():
    # test constructor
    @attr.attrs
    class Foo:
        var = xs.any_object()

    with pytest.raises(TypeError):
        # any_object variable not in contructor (intent='out')
        Foo(var=2)


def test_foreign():
    var = attr.fields(ExampleProcess).out_foreign_var
    ref_var = attr.fields(AnotherProcess).another_var

    for k in ("description", "attrs"):
        assert var.metadata[k] == ref_var.metadata[k]

    # test constructor
    @attr.attrs
    class Foo:
        some_var = xs.foreign(SomeProcess, "some_var")
        another_var = xs.foreign(AnotherProcess, "another_var", intent="out")

    assert Foo(some_var=2).some_var == 2

    with pytest.raises(TypeError):
        # intent='out' not in constructor
        Foo(another_var=2)


def test_global_ref():
    with pytest.raises(ValueError, match="intent='inout' is not supported.*"):
        xs.global_ref("some_var", intent="inout")

    # test constructor
    @attr.attrs
    class Foo:
        some_var = xs.global_ref("some_var")
        another_var = xs.global_ref("another_var", intent="out")

    assert Foo(some_var=2).some_var == 2

    with pytest.raises(TypeError):
        # intent='out' not in constructor
        Foo(another_var=2)


def test_group():
    @attr.attrs
    class Foo:
        bar = xs.group("g")

    # test init with default tuple value
    foo = Foo()
    assert foo.bar == tuple()


def test_group_dict():
    @attr.attrs
    class Foo:
        bar = xs.group_dict("g")

    # test init with default dict value
    foo = Foo()
    assert foo.bar == dict()
