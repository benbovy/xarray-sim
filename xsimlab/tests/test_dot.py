import re
import os
from errno import ENOENT

import pytest

pytest.importorskip("graphviz")
try:
    from IPython.display import Image, SVG

    ipython_installed = True
except ImportError:
    Image = (None,)
    SVG = None
    ipython_installed = False

from xsimlab.dot import to_graphviz, dot_graph, _hash_variable
from xsimlab.utils import variables_dict
import xsimlab as xs

# need to parse elements of graphivz's Graph object
g_node_label_re = re.compile(r'.*\[label=([\w<>\\"]*?)\s+.*\]')
g_edge_labels_re = re.compile(r"\s*([-\w]*?)\s+->\s+([-\w]*?)\s+.*]")


def _get_node_label(line):
    m = g_node_label_re.match(line)
    if m:
        return m.group(1)


def _get_edge_labels(line):
    m = g_edge_labels_re.match(line)
    if m:
        return m.group(1, 2)


def _get_graph_nodes(g):
    return list(filter(None, map(_get_node_label, g.body)))


def _get_graph_edges(g):
    return list(filter(None, map(_get_edge_labels, g.body)))


def _ensure_not_exists(filename):
    """
    Ensure that a file does not exist.
    """
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != ENOENT:
            raise


def test_to_graphviz(model):
    g = to_graphviz(model, show_feedbacks=False)
    actual_nodes = _get_graph_nodes(g)
    actual_edges = _get_graph_edges(g)
    expected_nodes = list(model)
    expected_edges = [
        (dep_p_name, p_name)
        for p_name, p_deps in model.dependent_processes.items()
        for dep_p_name in p_deps
    ]
    assert sorted(actual_nodes) == sorted(expected_nodes)
    assert set(actual_edges) == set(expected_edges)

    g = to_graphviz(model, show_inputs=True)
    actual_nodes = _get_graph_nodes(g)
    actual_edges = _get_graph_edges(g)
    expected_nodes += [var_name for _, var_name in model.input_vars]
    expected_edges += [
        (_hash_variable(variables_dict(type(model[p_name]))[var_name]), p_name)
        for p_name, var_name in model.input_vars
    ]
    assert sorted(actual_nodes) == sorted(expected_nodes)
    assert set(actual_edges) == set(expected_edges)

    g = to_graphviz(model, show_variables=True)
    actual_nodes = _get_graph_nodes(g)
    expected_nodes = list(model) + [var_name for _, var_name in model.all_vars]
    assert sorted(actual_nodes) == sorted(expected_nodes)

    g = to_graphviz(model, show_only_variable=("profile", "u"))
    actual_nodes = _get_graph_nodes(g)
    expected_nodes = list(model) + ["u"] * 3
    assert sorted(actual_nodes) == sorted(expected_nodes)


def test_to_graphviz_attributes(model):
    assert to_graphviz(model).graph_attr["rankdir"] == "LR"
    assert to_graphviz(model, rankdir="BT").graph_attr["rankdir"] == "BT"


# create processes for feedback edges test -> also for show_stages
@xs.process
class In1:
    v = xs.variable()

    def run_step(self):
        pass


@xs.process
class In2:
    v = xs.foreign(In1, "v")

    def finalize_step(self):
        pass


@xs.process
class In3:
    v = xs.foreign(In1, "v")

    def run_step(self):
        pass


@xs.process
class InNot:

    v = xs.foreign(In1, "v")


@xs.process
class Inout1:
    v = xs.foreign(In1, "v", intent="inout")

    def run_step(self):
        pass


@xs.process
class Inout2:
    v = xs.foreign(In1, "v", intent="inout")

    def finalize_step(self):
        pass


@xs.process
class Out1:
    v = xs.foreign(In1, "v", intent="out")

    def run_step(self):
        pass


def test_feedback_edges():
    #  /< - - - - - - \<-test
    # in1->io1->in3->io2
    # in_not/         /
    #    in2< - - - -/<-test

    model = xs.Model(
        {
            "in1": In1,
            "in2": In2,
            "in_not": InNot,
            "in3": In3,
            "io1": Inout1,
            "io2": Inout2,
        },
        custom_dependencies={
            "io2": "in3",
            "in3": "io1",
            "io1": {"in2", "in1", "in_not"},
        },
    )
    g = to_graphviz(model)
    actual_edges = _get_graph_edges(g)
    expected_edges = [
        (dep_p_name, p_name)
        for p_name, p_deps in model.dependent_processes.items()
        for dep_p_name in p_deps
    ] + [("io2", "in1"), ("io2", "in2")]
    assert set(actual_edges) == set(expected_edges)

    # /<- - - - - - - - \
    # out-in1->io1->in3->io2
    #   in_not/
    #      in2

    model = xs.Model(
        {
            "out1": Out1,
            "in1": In1,
            "in2": In2,
            "in3": In3,
            "io1": Inout1,
            "io2": Inout2,
        },
        custom_dependencies={"io2": "in3", "in3": "io1", "io1": {"in2", "in1"}},
    )
    g = to_graphviz(model)
    actual_edges = _get_graph_edges(g)
    expected_edges = [
        (dep_p_name, p_name)
        for p_name, p_deps in model.dependent_processes.items()
        for dep_p_name in p_deps
    ] + [("io2", "out1")]
    assert set(actual_edges) == set(expected_edges)


def test_show_stages():
    # we don't need to add custom dependencies,
    # with only in and inout processes, no edges are added and processes are
    # added in the order they are declared in the  model.
    #             r
    # in1->inout1->inout2->in3
    #     g    g\   /g    g
    #            in2
    model = xs.Model(
        {"in1": In1, "inout1": Inout1, "in2": In2, "inout2": Inout2, "in3": In3},
        strict_order_check=False,
    )
    g = to_graphviz(model, show_variable_stages=("in1", "v"))
    expected_edges = [
        ("inout1", "inout2"),
        ("in1", "inout1"),
        ("inout1", "in2"),
        ("in2", "inout2"),
        ("inout2", "in3"),
    ]


@pytest.mark.skipif(not ipython_installed, reason="IPython is not installed")
@pytest.mark.parametrize(
    "format,typ",
    [
        ("png", Image),
        pytest.param(
            "jpeg",
            Image,
            marks=pytest.mark.xfail(reason="jpeg not always supported in dot"),
        ),
        ("dot", type(None)),
        ("pdf", type(None)),
        ("svg", SVG),
    ],
)
def test_dot_graph(model, tmpdir, format, typ):
    # Use a name that the shell would interpret specially to ensure that we're
    # not vulnerable to shell injection when interacting with `dot`.
    filename = str(tmpdir.join("$(touch should_not_get_created.txt)"))

    target = ".".join([filename, format])
    _ensure_not_exists(target)
    try:
        result = dot_graph(model, filename=filename, format=format)

        assert not os.path.exists("should_not_get_created.txt")
        assert os.path.isfile(target)
        assert isinstance(result, typ)
    finally:
        _ensure_not_exists(target)

    # format supported by graphviz but not by IPython
    with pytest.raises(ValueError) as excinfo:
        dot_graph(model, filename=filename, format="ps")
    assert "Unknown format" in str(excinfo.value)


def test_dot_graph_no_ipython(model):
    try:
        import IPython.display  # noqa
    except ImportError:
        result = dot_graph(model)
        assert result is None


@pytest.mark.skipif(not ipython_installed, reason="IPython is not installed")
@pytest.mark.parametrize(
    "format,typ",
    [
        ("png", Image),
        pytest.param(
            "jpeg",
            Image,
            marks=pytest.mark.xfail(reason="jpeg not always supported in dot"),
        ),
        ("dot", type(None)),
        ("pdf", type(None)),
        ("svg", SVG),
    ],
)
def test_dot_graph_no_filename(tmpdir, model, format, typ):
    before = tmpdir.listdir()
    result = dot_graph(model, filename=None, format=format)
    # We shouldn't write any files if filename is None.
    after = tmpdir.listdir()
    assert before == after
    assert isinstance(result, typ)


@pytest.mark.skipif(not ipython_installed, reason="IPython is not installed")
def test_filenames_and_formats(model):

    # Test with a variety of user provided args
    filenames = [
        "modelpdf",
        "model.pdf",
        "model.pdf",
        "modelpdf",
        "model.pdf.svg",
    ]
    formats = ["svg", None, "svg", None, None]
    targets = [
        "modelpdf.svg",
        "model.pdf",
        "model.pdf.svg",
        "modelpdf.png",
        "model.pdf.svg",
    ]

    result_types = {
        "png": Image,
        "pdf": type(None),
        "svg": SVG,
    }

    for filename, format, target in zip(filenames, formats, targets):
        expected_result_type = result_types[target.split(".")[-1]]
        result = dot_graph(model, filename=filename, format=format)
        assert os.path.isfile(target)
        assert isinstance(result, expected_result_type)
        _ensure_not_exists(target)
