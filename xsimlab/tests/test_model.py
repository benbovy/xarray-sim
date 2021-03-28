import attr
import numpy as np
import pytest

import xsimlab as xs
from xsimlab.process import get_process_cls
from xsimlab.model import get_model_variables
from xsimlab.tests.fixture_model import AddOnDemand, InitProfile, Profile
from xsimlab.utils import Frozen
from xsimlab.variable import VarType


def test_get_model_variables(model):
    idx_vars = get_model_variables(model, var_type=VarType.INDEX)

    assert idx_vars == model.index_vars


class TestModelBuilder:
    def test_bind_processes(self, model):
        assert model._processes["profile"].__xsimlab_model__ is model
        assert model._processes["profile"].__xsimlab_name__ == "profile"

    def test_set_state(self, model):
        # test state bound to processes
        model.state[("init_profile", "n_points")] = 10
        assert model.init_profile.n_points == 10

    def test_create_variable_cache(self, model):
        actual = model._var_cache[("init_profile", "n_points")]

        assert actual["name"] == "init_profile__n_points"
        assert (
            actual["attrib"]
            is attr.fields_dict(model["init_profile"].__class__)["n_points"]
        )
        assert actual["metadata"] == attr.fields_dict(InitProfile)["n_points"].metadata
        assert actual["value"] is None

    @pytest.mark.parametrize(
        "p_name,expected_state_keys,expected_od_keys",
        [
            (
                "init_profile",
                {
                    "n_points": ("init_profile", "n_points"),
                    "x": ("init_profile", "x"),
                    "u": ("profile", "u"),
                },
                {},
            ),
            (
                "profile",
                {"u": ("profile", "u"), "u_diffs": [("roll", "u_diff")]},
                {"u_diffs": [("add", "u_diff")], "u_opp": ("profile", "u_opp")},
            ),
            (
                "roll",
                {
                    "shift": ("roll", "shift"),
                    "u": ("profile", "u"),
                    "u_diff": ("roll", "u_diff"),
                },
                {},
            ),
            (
                "add",
                {"offset": ("add", "offset")},
                {"u_diff": ("add", "u_diff")},
            ),
        ],
    )
    def test_set_process_keys(
        self, model, p_name, expected_state_keys, expected_od_keys
    ):
        p_obj = model._processes[p_name]
        actual_state_keys = p_obj.__xsimlab_state_keys__
        actual_od_keys = p_obj.__xsimlab_od_keys__

        # key order is not ensured for group variables
        if isinstance(expected_state_keys, list):
            actual_state_keys = set(actual_state_keys)
            expected_state_keys = set(expected_state_keys)
        if isinstance(expected_od_keys, list):
            actual_od_keys = set(actual_od_keys)
            expected_od_keys = set(expected_od_keys)

        assert actual_state_keys == expected_state_keys
        assert actual_od_keys == expected_od_keys

    def test_object_variable(self):
        @xs.process
        class P:
            obj = xs.any_object()

        m = xs.Model({"p": P})

        assert m.p.__xsimlab_state_keys__["obj"] == ("p", "obj")

    def test_multiple_groups(self):
        @xs.process
        class A:
            v = xs.variable(groups=["g1", "g2"])

        @xs.process
        class B:
            g1 = xs.group("g1")
            g2 = xs.group("g2")

        m = xs.Model({"a": A, "b": B})

        assert m.b.__xsimlab_state_keys__["g1"] == [("a", "v")]
        assert m.b.__xsimlab_state_keys__["g2"] == [("a", "v")]

    def test_get_all_variables(self, model):
        assert all([len(t) == 2 for t in model.all_vars])
        assert all([p_name in model for p_name, _ in model.all_vars])
        assert ("profile", "u") in model.all_vars

    def test_ensure_no_intent_conflict(self, model):
        @xs.process
        class Foo:
            u = xs.foreign(Profile, "u", intent="out")

        with pytest.raises(ValueError, match=r"Conflict.*"):
            model.update_processes({"foo": Foo})

    def test_get_input_variables(self, model):
        expected = {
            ("init_profile", "n_points"),
            ("roll", "shift"),
            ("add", "offset"),
        }

        assert set(model.input_vars) == expected

    def test_get_process_dependencies(self, model):
        expected = {
            "init_profile": [],
            "profile": ["init_profile", "add", "roll"],
            "roll": ["init_profile"],
            "add": [],
        }

        actual = model.dependent_processes

        for p_name in expected:
            # order of dependencies is not ensured
            assert set(actual[p_name]) == set(expected[p_name])

    def test_get_process_dependencies_custom(self, model):
        @xs.process
        class A:
            pass

        @xs.process
        class B:
            pass

        @xs.process
        class C:
            pass

        actual = xs.Model(
            {"a": A, "b": B}, custom_dependencies={"a": "b"}
        ).dependent_processes
        expected = {"a": ["b"], "b": []}

        for p_name in expected:
            assert set(actual[p_name]) == set(expected[p_name])

        # also test with a list
        actual = xs.Model(
            {"a": A, "b": B, "c": C}, custom_dependencies={"a": ["b", "c"]}
        ).dependent_processes
        expected = {"a": ["b", "c"], "b": [], "c": []}

        for p_name in expected:
            assert set(actual[p_name]) == set(expected[p_name])

    @pytest.mark.parametrize(
        "p_name,dep_p_name",
        [
            ("profile", "init_profile"),
            ("profile", "add"),
            ("profile", "roll"),
            ("roll", "init_profile"),
        ],
    )
    def test_sort_processes(self, model, p_name, dep_p_name):
        p_ordered = list(model)
        assert p_ordered.index(p_name) > p_ordered.index(dep_p_name)

    def test_sort_processes_cycle(self, model):
        @xs.process
        class Foo:
            in_var = xs.variable()
            out_var = xs.variable(intent="out")

        @xs.process
        class Bar:
            in_foreign = xs.foreign(Foo, "out_var")
            out_foreign = xs.foreign(Foo, "in_var", intent="out")

        with pytest.raises(RuntimeError, match=r"Cycle detected.*"):
            xs.Model({"foo": Foo, "bar": Bar})

    def test_strict_check(self):
        # also give the variable different names
        @xs.process
        class Inout1:
            v = xs.variable(intent="inout")

        @xs.process
        class Inout2:
            va = xs.foreign(Inout1, "v", intent="inout")

        @xs.process
        class Inout3:
            var = xs.foreign(Inout1, "v", intent="inout")

        @xs.process
        class In1:
            var = xs.foreign(Inout1, "v")

        # io # equivalent to # io-..-io
        # in #               #  in              #where any io can be in in deps
        with pytest.raises(RuntimeError, match="some input processes do not"):
            xs.Model({"io1": Inout1, "in1": In1}, strict_order_check=True)
        # io-in #eq. to io-..-io-in
        xs.Model(
            {"io1": Inout1, "in1": In1},
            strict_order_check=True,
            custom_dependencies={"in1": "io1"},
        )
        # in-io #eq to in-io-..-io
        xs.Model(
            {"io1": Inout1, "in1": In1},
            strict_order_check=True,
            custom_dependencies={"io1": "in1"},
        )
        # io
        # io
        with pytest.raises(RuntimeError, match="has two branch dependencies"):
            xs.Model({"io1": Inout1, "io2": Inout2}, strict_order_check=True)
        # io-io
        xs.Model(
            {"io1": Inout1, "io2": Inout2},
            custom_dependencies={"io2": "io1"},
            strict_order_check=True,
        )
        # io-io
        #    /
        #  in
        with pytest.raises(RuntimeError, match="io2 updates it and depends"):
            xs.Model(
                {"io1": Inout1, "io2": Inout2, "in1": In1},
                custom_dependencies={"io2": ["io1", "in1"]},
                strict_order_check=True,
            )
        # io io
        # \ /
        # in
        xs.Model(
            {"io1": Inout1, "io2": Inout2, "in1": In1},
            custom_dependencies={"io2": "in1", "in1": "io1"},
        )

        # the following is a bit arbitrary which raises: 2 or 3 based on the ordering of dicts
        # io2-io1
        #     /
        #  io3   This raises in first tree traversal: should be equivalent to
        with pytest.raises(RuntimeError, match="has two branch dependencies"):
            xs.Model(
                {"io1": Inout1, "io2": Inout2, "io3": Inout3},
                custom_dependencies={"io1": ["io2", "io3"]},
                strict_order_check=True,
            )

        # io-io
        #  \
        #  io
        with pytest.raises(RuntimeError, match="order of inout process"):
            xs.Model(
                {"io1": Inout1, "io2": Inout2, "io3": Inout3},
                custom_dependencies={"io1": "io2", "io3": "io2"},
                strict_order_check=True,
            )

    def test_strict_check_multiple_vars_in_process(self):
        # in|->|io|->|in|->|io| - foo variable
        #   |  |in|->|io|->|in| - bar variable
        @xs.process
        class Out:
            foo = xs.on_demand()
            bar = xs.variable(intent="out")

            @foo.compute
            def method(self):
                pass

        @xs.process
        class FooInBarInout:
            foo = xs.foreign(Out, "foo")
            bar = xs.foreign(Out, "bar", intent="inout")

        @xs.process
        class FooInoutBarIn:
            foo = xs.foreign(Out, "foo")
            bar = xs.foreign(Out, "bar", intent="inout")

        @xs.process
        class FooIn:
            foo = xs.foreign(Out, "foo")

        @xs.process
        class BarInFooInout:
            bar = xs.foreign(Out, "bar")
            foo = xs.foreign(Out, "foo", intent="inout")

        xs.Model(
            {
                "out": Out,
                "foo_in_bar_inout": FooInBarInout,
                "foo_inout_bar_in": FooInoutBarIn,
                "foo_in": FooIn,
                "boo_in_foo_inout": BarInFooInout,
            },
            custom_dependencies={
                "boo_in_foo_inout": "foo_in_bar_inout",
                "foo_in_bar_inout": "foo_inout_bar_in",
                "foo_inout_bar_in": "foo_in",
            },
            strict_order_check=True,
        )

    def test_process_inheritance(self, model):
        @xs.process
        class InheritedProfile(Profile):
            pass

        new_model = model.update_processes({"profile": InheritedProfile})

        assert type(new_model["profile"]) is get_process_cls(InheritedProfile)
        assert isinstance(new_model["profile"], Profile)

        with pytest.raises(ValueError, match=r".*multiple processes.*"):
            model.update_processes({"profile2": InheritedProfile})


class TestModel:
    def test_constructor(self):
        with pytest.raises(KeyError) as excinfo:
            xs.Model({"init_profile": InitProfile})
        assert "Process class 'Profile' missing" in str(excinfo.value)

        # test empty model
        assert len(xs.Model({})) == 0

    def test_process_dict_vs_attr_access(self, model):
        assert model["profile"] is model.profile

    def test_all_vars_dict(self, model):
        assert all([p_name in model for p_name in model.all_vars_dict])
        assert all(
            [isinstance(p_vars, list) for p_vars in model.all_vars_dict.values()]
        )
        assert "u" in model.all_vars_dict["profile"]

    def test_index_vars_dict(self, model):
        assert all([p_name in model for p_name in model.index_vars_dict])
        assert all(
            [isinstance(p_vars, list) for p_vars in model.index_vars_dict.values()]
        )
        assert "x" in model.index_vars_dict["init_profile"]

    def test_input_vars_dict(self, model):
        assert all([p_name in model for p_name in model.input_vars_dict])
        assert all(
            [isinstance(p_vars, list) for p_vars in model.input_vars_dict.values()]
        )
        assert "n_points" in model.input_vars_dict["init_profile"]

    def test_update_state(self, model):
        arr = np.array([1, 2, 3, 4])

        input_vars = {
            ("init_profile", "n_points"): 10.2,
            ("add", "offset"): arr,
            ("not-a-model", "input"): 0,
        }

        model.update_state(input_vars, ignore_static=True, ignore_invalid_keys=True)

        # test converted value
        assert model.state[("init_profile", "n_points")] == 10
        assert type(model.state[("init_profile", "n_points")]) is int

        # test copy
        np.testing.assert_array_equal(model.state[("add", "offset")], arr)
        assert model.state[("add", "offset")] is not arr

        # test invalid key ignored
        assert ("not-a-model", "input") not in model.state

        # test validate
        with pytest.raises(TypeError, match=r".*'int'.*"):
            model.update_state({("roll", "shift"): 2.5})

        # test errors
        with pytest.raises(ValueError, match=r".* static variable .*"):
            model.update_state(
                input_vars, ignore_static=False, ignore_invalid_keys=True
            )

        with pytest.raises(KeyError, match=r".* not a valid input variable .*"):
            model.update_state(
                input_vars, ignore_static=True, ignore_invalid_keys=False
            )

    def test_update_cache(self, model):
        model.state[("init_profile", "n_points")] = 10
        model.update_cache(("init_profile", "n_points"))

        assert model.cache[("init_profile", "n_points")]["value"] == 10

        # test on demand variables
        model.state[("add", "offset")] = 1
        model.update_cache(("add", "u_diff"))

        assert model.cache[("add", "u_diff")]["value"] == 1

    def test_validate(self, model):
        model.state[("roll", "shift")] = 2.5

        with pytest.raises(TypeError, match=r".*'int'.*"):
            model.validate(["roll"])

    def test_clone(self, model):
        cloned = model.clone()

        for p_name in model:
            assert cloned[p_name] is not model[p_name]

    def test_update_processes(self, no_init_model, model):
        m = no_init_model.update_processes(
            {"add": AddOnDemand, "init_profile": InitProfile}
        )
        assert m == model

    @pytest.mark.parametrize("p_names", ["add", ["add"]])
    def test_drop_processes(self, no_init_model, simple_model, p_names):
        m = no_init_model.drop_processes(p_names)
        assert m == simple_model

    def test_drop_processes_custom(self):
        @xs.process
        class A:
            pass

        @xs.process
        class B:
            pass

        @xs.process
        class C:
            pass

        @xs.process
        class D:
            pass

        model = xs.Model(
            {"a": A, "b": B, "c": C, "d": D},
            custom_dependencies={"d": "c", "c": "b", "b": "a"},
        )
        model = model.drop_processes(["b", "c"])
        assert model.dependent_processes["d"] == ["a"]

    def test_visualize(self, model):
        pytest.importorskip("graphviz")
        ipydisp = pytest.importorskip("IPython.display")

        result = model.visualize()
        assert isinstance(result, ipydisp.Image)

        result = model.visualize(show_inputs=True)
        assert isinstance(result, ipydisp.Image)

        result = model.visualize(show_variables=True)
        assert isinstance(result, ipydisp.Image)

        result = model.visualize(show_only_variable=("profile", "u"))
        assert isinstance(result, ipydisp.Image)

    def test_context_manager(self):
        m1 = xs.Model({})
        m2 = xs.Model({})

        with pytest.raises(ValueError, match=r"There is already a model object.*"):
            with m1, m2:
                pass

    def test_repr(self, simple_model, simple_model_repr):
        assert repr(simple_model) == simple_model_repr


def test_on_demand_cache():
    @xs.process
    class P1:
        var = xs.on_demand(dims="x")
        cached_var = xs.on_demand(dims="x")

        @var.compute
        def _compute_var(self):
            return np.random.rand(10)

        @cached_var.compute(cache=True)
        def _compute_cached_var(self):
            return np.random.rand(10)

    @xs.process
    class P2:
        var = xs.foreign(P1, "var")
        cached_var = xs.foreign(P1, "cached_var")
        view = xs.variable(dims="x", intent="out")
        cached_view = xs.variable(dims="x", intent="out")

        def run_step(self):
            self.view = self.var
            self.cached_view = self.cached_var

    @xs.process
    class P3:
        p1_view = xs.foreign(P1, "var")
        p1_cached_view = xs.foreign(P1, "cached_var")
        p2_view = xs.foreign(P2, "view")
        p2_cached_view = xs.foreign(P2, "cached_view")

        def initialize(self):
            self._p1_cached_view_init = self.p1_cached_view

        def run_step(self):
            # P1.var's compute method called twice
            assert not np.all(self.p1_view == self.p2_view)
            # P1.cached_var's compute method called once
            assert self.p1_cached_view is self.p2_cached_view
            # check cache cleared between simulation stages
            assert not np.all(self.p1_cached_view == self._p1_cached_view_init)

    model = xs.Model({"p1": P1, "p2": P2, "p3": P3})
    model.execute("initialize", {})
    model.execute("run_step", {})


def test_global_variable():
    @xs.process
    class Foo:
        var = xs.variable(global_name="global_var")
        idx = xs.index(dims="x", global_name="global_idx")
        obj = xs.any_object(global_name="global_obj")

        def initialize(self):
            self.idx = np.array([1, 1])
            self.obj = 2

    @xs.process
    class Bar:
        var = xs.global_ref("global_var")
        idx = xs.global_ref("global_idx")
        obj = xs.global_ref("global_obj")

        actual = xs.variable(intent="out")

        def initialize(self):
            self.actual = self.var + self.obj * np.sum(self.idx)

    @xs.process
    class Baz:
        # foreign pointing to global reference Bar.var
        # --> must pass through and actually points to Foo.var
        var = xs.foreign(Bar, "var", intent="out")

        def initialize(self):
            self.var = 1

    model = xs.Model({"foo": Foo, "bar": Bar, "baz": Baz})
    model.execute("initialize", {})

    assert model.state[("foo", "var")] == 1
    assert model.state[("bar", "actual")] == 5

    # -- test errors
    @xs.process
    class NotFound:
        var = xs.global_ref("missing")

    with pytest.raises(
        KeyError, match="No variable with global name 'missing' found.*"
    ):
        xs.Model({"foo": Foo, "not_found": NotFound})

    @xs.process
    class Duplicate:
        var = xs.variable(global_name="global_var")

    with pytest.raises(ValueError, match="Found multiple variables with global name.*"):
        xs.Model({"foo": Foo, "bar": Bar, "dup": Duplicate})


def test_group_dict_variable():
    @xs.process
    class Foo:
        a = xs.variable(groups="g", intent="out")

        def initialize(self):
            self.a = 1

    @xs.process
    class Bar:
        b = xs.variable(groups="g", intent="out")

        def initialize(self):
            self.b = 2

    @xs.process
    class Baz:
        c = xs.group_dict("g")
        actual = xs.variable(intent="out")

        def initialize(self):
            self.actual = self.c

    model = xs.Model({"foo": Foo, "bar": Bar, "baz": Baz})
    model.execute("initialize", {})

    assert model.state[("baz", "actual")] == Frozen({("foo", "a"): 1, ("bar", "b"): 2})


def test_main_clock_access():
    @xs.process
    class Foo:
        a = xs.variable(intent="out", dims=xs.MAIN_CLOCK)
        b = xs.variable(intent="out", dims=xs.MAIN_CLOCK)

        @xs.runtime(args=["main_clock_values", "main_clock_dataarray"])
        def initialize(self, clock_values, clock_array):
            self.a = clock_values * 2
            np.testing.assert_equal(self.a, [0, 2, 4, 6])
            self.b = clock_array * 2
            assert clock_array.dims[0] == "clock"
            assert all(clock_array[clock_array.dims[0]].data == [0, 1, 2, 3])

        @xs.runtime(args=["step_delta", "step"])
        def run_step(self, dt, n):
            assert self.a[n] == 2 * n
            self.a[n] += 1

    model = xs.Model({"foo": Foo})
    ds_in = xs.create_setup(
        model=model,
        clocks={"clock": range(4)},
        input_vars={},
        output_vars={"foo__a": None},
    )
    ds_out = ds_in.xsimlab.run(model=model)
    assert all(ds_out.foo__a.data == [1, 3, 5, 6])

    # test for error when another dim has the same name as xs.MAIN_CLOCK
    @xs.process
    class DoubleMainClockDim:
        a = xs.variable(intent="out", dims=("clock", xs.MAIN_CLOCK))

        def initialize(self):
            self.a = [[1, 2, 3], [3, 4, 5]]

        def run_step(self):
            self.a += self.a

    model = xs.Model({"foo": DoubleMainClockDim})
    with pytest.raises(ValueError, match=r"Main clock:*"):
        xs.create_setup(
            model=model,
            clocks={"clock": [0, 1, 2, 3]},
            input_vars={},
            output_vars={"foo__a": None},
        ).xsimlab.run(model)

    # test for error when trying to put xs.MAIN_CLOCK as a dim in an input var
    with pytest.raises(
        ValueError, match="Do not pass xs.MAIN_CLOCK into input vars dimensions"
    ):
        a = xs.variable(intent="in", dims=xs.MAIN_CLOCK)

    with pytest.raises(
        ValueError, match="Do not pass xs.MAIN_CLOCK into input vars dimensions"
    ):
        b = xs.variable(intent="in", dims=(xs.MAIN_CLOCK,))
    with pytest.raises(
        ValueError, match="Do not pass xs.MAIN_CLOCK into input vars dimensions"
    ):
        c = xs.variable(intent="in", dims=["a", ("a", xs.MAIN_CLOCK)])
