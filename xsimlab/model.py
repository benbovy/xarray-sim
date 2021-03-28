from collections import OrderedDict, defaultdict
import copy
import time

import attr
import dask
from dask.distributed import Client

from .variable import VarIntent, VarType
from .process import (
    filter_variables,
    get_process_cls,
    get_target_variable,
    RuntimeSignal,
    SimulationStage,
)
from .utils import AttrMapping, Frozen, variables_dict
from .formatting import repr_model


def _flatten_keys(key_seq):
    """returns a flat list of keys, i.e., ``('foo', 'bar')`` tuples, from
    a nested sequence.

    """
    flat_keys = []

    for key in key_seq:
        if not isinstance(key, tuple):
            flat_keys += _flatten_keys(key)
        else:
            flat_keys.append(key)

    return flat_keys


def get_model_variables(p_mapping, **kwargs):
    """Get variables in the model (processes mapping) as a list of
    ``(process_name, var_name)`` tuples.

    **kwargs may be used to return only a subset of the variables.

    """
    var_keys = []

    for p_name, proc in p_mapping.items():
        var_keys += [
            (p_name, var_name) for var_name in filter_variables(proc, **kwargs)
        ]

    return var_keys


def get_reverse_lookup(processes_cls):
    """Return a dictionary with process classes as keys and process names
    as values.

    Additionally, the returned dictionary maps all parent classes
    to one (str) or several (list) process names.

    """
    reverse_lookup = defaultdict(list)

    for p_name, p_cls in processes_cls.items():
        # exclude `object` base class from lookup
        for cls in p_cls.mro()[:-1]:
            reverse_lookup[cls].append(p_name)

    return {k: v[0] if len(v) == 1 else v for k, v in reverse_lookup.items()}


def get_global_refs(processes_cls):
    """Return a dictionary with global names as keys and
    ('process_name', var) tuples (or lists of those tuples) as values.

    """
    temp_refs = defaultdict(list)

    for p_name, p_cls in processes_cls.items():
        for var in variables_dict(p_cls).values():
            global_name = var.metadata.get("global_name")

            if var.metadata["var_type"] != VarType.GLOBAL and global_name is not None:
                temp_refs[global_name].append((p_name, var))

    global_refs = {k: v if len(v) > 1 else v[0] for k, v in temp_refs.items()}

    return global_refs


class _ModelBuilder:
    """Used to iteratively build a new model.

    This builder implements the following tasks:

    - Attach the model instance to each process and assign their given
      name in model.
    - Create a "state", i.e., a mapping used to store active simulation data
    - Create a cache for fastpath access to the (meta)data of all variables
      defined in the model
    - Define for each variable of the model its corresponding key
      (in state or on-demand)
    - Find variables that are model inputs
    - Find process dependencies and sort processes (DAG)
    - Find the processes that implement the method relative to each
      step of a simulation

    """

    def __init__(self, processes_cls):
        self._processes_cls = processes_cls
        self._processes_obj = {k: cls() for k, cls in processes_cls.items()}

        self._reverse_lookup = get_reverse_lookup(processes_cls)

        self._all_vars = get_model_variables(processes_cls)
        self._global_vars = get_model_variables(processes_cls, var_type=VarType.GLOBAL)
        self._global_refs = get_global_refs(processes_cls)
        self._input_vars = None

        self._dep_processes = None
        self._sorted_processes = None

        # a cache for group keys
        self._group_keys = {}

    def bind_processes(self, model_obj):
        for p_name, p_obj in self._processes_obj.items():
            p_obj.__xsimlab_model__ = model_obj
            p_obj.__xsimlab_name__ = p_name

    def set_state(self):
        state = {}

        # bind state to each process in the model
        for p_obj in self._processes_obj.values():
            p_obj.__xsimlab_state__ = state

        return state

    def create_variable_cache(self):
        """Create a cache for fastpath access to the (meta)data of all
        variables defined in the model.

        """
        var_cache = {}

        for p_name, p_cls in self._processes_cls.items():
            for v_name, attrib in variables_dict(p_cls).items():
                var_cache[(p_name, v_name)] = {
                    "name": f"{p_name}__{v_name}",
                    "attrib": attrib,
                    "metadata": attrib.metadata.copy(),
                    "value": None,
                }

        # retrieve/update metadata for global variables
        for key in self._global_vars:
            metadata = var_cache[key]["metadata"]
            _, ref_var = self._get_global_ref(var_cache[key]["attrib"])

            ref_metadata = {
                k: v for k, v in ref_var.metadata.items() if k not in metadata
            }
            metadata.update(ref_metadata)

        return var_cache

    def _get_global_ref(self, var):
        """Return the reference to a global variable as a ('process_name', var) tuple
        (check that a reference exists and is unique).

        """
        global_name = var.metadata["global_name"]
        ref = self._global_refs.get(global_name)

        if ref is None:
            raise KeyError(
                f"No variable with global name '{global_name}' found in model"
            )

        elif isinstance(ref, list):
            raise ValueError(
                f"Found multiple variables with global name '{global_name}' in model: "
                ", ".join([str(r) for r in ref])
            )

        return ref

    def _get_foreign_ref(self, p_name, var):
        """Return the reference to a foreign variable as a ('process_name', var) tuple
        (check that a reference exists and is unique).

        """
        target_p_cls, target_var = get_target_variable(var)
        target_p_name = self._reverse_lookup.get(target_p_cls, None)

        if target_p_name is None:
            raise KeyError(
                f"Process class '{target_p_cls.__name__}' "
                "missing in Model but required "
                f"by foreign variable '{var.name}' "
                f"declared in process '{p_name}'"
            )

        elif isinstance(target_p_name, list):
            raise ValueError(
                "Process class {!r} required by foreign variable '{}.{}' "
                "is used (possibly via one its child classes) by multiple "
                "processes: {}".format(
                    target_p_cls.__name__,
                    p_name,
                    var.name,
                    ", ".join(["{!r}".format(n) for n in target_p_name]),
                )
            )

        # go through global reference
        if target_var.metadata["var_type"] == VarType.GLOBAL:
            target_p_name, target_var = self._get_global_ref(target_var)

        return target_p_name, target_var

    def _get_var_key(self, p_name, var):
        """Get state and/or on-demand keys for variable `var` declared in
        process `p_name`.

        Returned key(s) are either None (if no key), a tuple or a list
        of tuples (for group variables).

        A key tuple looks like ``('foo', 'bar')`` where 'foo' is the
        name of any process in the model and 'bar' is the name of a
        variable declared in that process.

        """
        state_key = None
        od_key = None

        var_type = var.metadata["var_type"]

        if var_type in (VarType.VARIABLE, VarType.INDEX, VarType.OBJECT):
            state_key = (p_name, var.name)

        elif var_type == VarType.ON_DEMAND:
            od_key = (p_name, var.name)

        elif var_type == VarType.FOREIGN:
            state_key, od_key = self._get_var_key(*self._get_foreign_ref(p_name, var))

        elif var_type == VarType.GLOBAL:
            state_key, od_key = self._get_var_key(*self._get_global_ref(var))

        elif var_type in (VarType.GROUP, VarType.GROUP_DICT):
            var_group = var.metadata["group"]
            state_key, od_key = self._get_group_var_keys(var_group)

        return state_key, od_key

    def _get_group_var_keys(self, group):
        """Get from cache or find model-wise state and on-demand keys
        for all variables related to a group (except group variables).

        """
        if group in self._group_keys:
            return self._group_keys[group]

        state_keys = []
        od_keys = []

        for p_name, p_obj in self._processes_obj.items():
            for var in filter_variables(p_obj, group=group).values():
                state_key, od_key = self._get_var_key(p_name, var)

                if state_key is not None:
                    state_keys.append(state_key)
                if od_key is not None:
                    od_keys.append(od_key)

        self._group_keys[group] = state_keys, od_keys

        return state_keys, od_keys

    def set_process_keys(self):
        """Find state and/or on-demand keys for all variables in a model and
        store them in their respective process, i.e., the following
        attributes:

        __xsimlab_state_keys__  (state keys)
        __xsimlab_od_keys__     (on-demand keys)

        """
        for p_name, p_obj in self._processes_obj.items():
            for var in filter_variables(p_obj).values():
                state_key, od_key = self._get_var_key(p_name, var)

                if state_key is not None:
                    p_obj.__xsimlab_state_keys__[var.name] = state_key
                if od_key is not None:
                    p_obj.__xsimlab_od_keys__[var.name] = od_key

    def ensure_no_intent_conflict(self):
        """Raise an error if more than one variable with
        intent='out' targets the same variable.

        """

        def filter_out(var):
            return (
                var.metadata["intent"] == VarIntent.OUT
                and var.metadata["var_type"] != VarType.ON_DEMAND
            )

        targets = defaultdict(list)

        for p_name, p_obj in self._processes_obj.items():
            for var in filter_variables(p_obj, func=filter_out).values():
                target_key = p_obj.__xsimlab_state_keys__.get(var.name)
                targets[target_key].append((p_name, var.name))

        conflicts = {k: v for k, v in targets.items() if len(v) > 1}

        if conflicts:
            conflicts_str = {
                k: " and ".join(["'{}.{}'".format(*i) for i in v])
                for k, v in conflicts.items()
            }
            msg = "\n".join(
                [f"'{'.'.join(k)}' set by: {v}" for k, v in conflicts_str.items()]
            )

            raise ValueError(f"Conflict(s) found in given variable intents:\n{msg}")

    def get_variables(self, **kwargs):
        if not len(kwargs):
            return self._all_vars
        else:
            return get_model_variables(self._processes_cls, **kwargs)

    def get_input_variables(self):
        """Get all input variables in the model as a list of
        ``(process_name, var_name)`` tuples.

        Model input variables meet the following conditions:

        - model-wise (i.e., in all processes), there is no variable with
          intent='out' targeting those variables (in state keys).
        - although group variables always have intent='in', they are not
          model inputs.

        """

        def filter_in(var):
            return (
                var.metadata["var_type"] != VarType.GROUP
                and var.metadata["var_type"] != VarType.GROUP_DICT
                and var.metadata["intent"] != VarIntent.OUT
            )

        def filter_out(var):
            return var.metadata["intent"] == VarIntent.OUT

        in_keys = []
        out_keys = []

        for p_obj in self._processes_obj.values():
            in_keys += [
                p_obj.__xsimlab_state_keys__.get(var.name)
                for var in filter_variables(p_obj, func=filter_in).values()
            ]
            out_keys += [
                p_obj.__xsimlab_state_keys__.get(var.name)
                for var in filter_variables(p_obj, func=filter_out).values()
            ]

        input_vars = [k for k in set(in_keys) - set(out_keys) if k is not None]

        # order consistent with variable and process declaration
        self._input_vars = [k for k in self.get_variables() if k in input_vars]

        return self._input_vars

    def get_processes_to_validate(self):
        """Return a dictionary where keys are each process of the model and
        values are lists of the names of other processes for which to trigger
        validators right after its execution.

        Useful for triggering validators of variables defined in other
        processes when new values are set through foreign variables.

        """
        processes_to_validate = {k: set() for k in self._processes_obj}

        for p_name, p_obj in self._processes_obj.items():
            out_foreign_vars = filter_variables(
                p_obj, var_type=VarType.FOREIGN, intent=VarIntent.OUT
            )

            for var in out_foreign_vars.values():
                pn, _ = p_obj.__xsimlab_state_keys__[var.name]
                processes_to_validate[p_name].add(pn)

        return {k: list(v) for k, v in processes_to_validate.items()}

    def get_process_dependencies(self, custom_dependencies={}):
        """Return a dictionary where keys are each process of the model and
        values are lists of the names of dependent processes (or empty
        lists for processes that have no dependencies).

        Process 1 depends on process 2 if the later declares a
        variable (resp. a foreign variable) with intent='out' that
        itself (resp. its target variable) is needed in process 1.

        """
        self._dep_processes = {k: set() for k in self._processes_obj}

        d_keys = {}  # all state/on-demand keys for each process

        for p_name, p_obj in self._processes_obj.items():
            d_keys[p_name] = _flatten_keys(
                [
                    p_obj.__xsimlab_state_keys__.values(),
                    p_obj.__xsimlab_od_keys__.values(),
                ]
            )

        # actually add custom dependencies
        for p_name, deps in custom_dependencies.items():
            self._dep_processes[p_name].update(deps)

        for p_name, p_obj in self._processes_obj.items():
            for var in filter_variables(p_obj, intent=VarIntent.OUT).values():
                if var.metadata["var_type"] == VarType.ON_DEMAND:
                    key = p_obj.__xsimlab_od_keys__[var.name]
                else:
                    key = p_obj.__xsimlab_state_keys__[var.name]

                for pn in self._processes_obj:
                    if pn != p_name and key in d_keys[pn]:
                        self._dep_processes[pn].add(p_name)

        self._dep_processes = {k: list(v) for k, v in self._dep_processes.items()}

        return self._dep_processes

    def _sort_processes(self):
        """Sort processes based on their dependencies (return a list of sorted
        process names).

        Stack-based depth-first search traversal.

        This is based on Tarjan's method for topological sorting.

        Part of the code below is copied and modified from:

        - dask 0.14.3 (Copyright (c) 2014-2015, Continuum Analytics, Inc.
          and contributors)
          Licensed under the BSD 3 License
          http://dask.pydata.org

        """
        ordered = []
        self._deps_dict = {p: set() for p in self._dep_processes}

        # Nodes whose descendents have been completely explored.
        # These nodes are guaranteed to not be part of a cycle.
        completed = set()

        # All nodes that have been visited in the current traversal.  Because
        # we are doing depth-first search, going "deeper" should never result
        # in visiting a node that has already been seen.  The `seen` and
        # `completed` sets are mutually exclusive; it is okay to visit a node
        # that has already been added to `completed`.
        seen = set()

        for key in self._dep_processes:
            if key in completed:
                continue
            nodes = [key]
            while nodes:
                # Keep current node on the stack until all descendants are
                # visited
                cur = nodes[-1]
                if cur in completed:
                    # Already fully traversed descendants of cur
                    nodes.pop()
                    continue
                seen.add(cur)

                # Add direct descendants of cur to nodes stack
                next_nodes = []
                for nxt in self._dep_processes[cur]:
                    if nxt in seen:
                        # Cycle detected!
                        cycle = [nxt]
                        while nodes[-1] != nxt:
                            cycle.append(nodes.pop())
                        cycle.append(nodes.pop())
                        cycle.reverse()
                        cycle = "->".join(cycle)
                        raise RuntimeError(f"Cycle detected in process graph: {cycle}")
                    if nxt in completed:
                        self._deps_dict[cur].add(nxt)
                        self._deps_dict[cur].update(self._deps_dict[nxt])
                    else:
                        next_nodes.append(nxt)

                if next_nodes:
                    nodes.extend(next_nodes)
                else:
                    # cur has no more descendants to explore,
                    # so we're done with it
                    ordered.append(cur)
                    completed.add(cur)
                    seen.remove(cur)
                    nodes.pop()

        return ordered

    def _strict_order_check(self):
        """
        IMPORTANT: _sort_processes should be run first
        checks if all inout variables and corresponding in variables are explicitly set in the dependencies
        Out variables always come first, since the get_process_dependencies checks for that.
        A well-behaved graph looks like: ``in0->inout1->in1->inout2->in2``
        """
        # create dictionaries with all inout variables and input variables
        inout_dict = {}  # dict of {key:{p1_name,p2_name}} for inout variables
        # TODO: improve this: the aim is to create a {key:{p1,p2,p3}} dict,
        # where p1,p2,p3 are process names that have the key var as inout, resp. in vars
        # some problems are that we can have on_demand and state varibles,
        # that key can return a tuple or list,
        for p_name, p_obj in self._processes_obj.items():
            # create {key:{p1_name,p2_name}} dicts for in and inout vars.
            for var in filter_variables(p_obj, intent=VarIntent.INOUT).values():
                state_key, od_key = self._get_var_key(p_name, var)
                if state_key is not None:
                    if not state_key in inout_dict:
                        inout_dict[state_key] = {p_name}
                    else:
                        inout_dict[state_key].add(p_name)
                if od_key is not None:
                    if not od_key in inout_dict:
                        inout_dict[od_key] = {p_name}
                    else:
                        inout_dict[od_key].add(p_name)

        in_dict = {key: set() for key in inout_dict}
        for p_name, p_obj in self._processes_obj.items():
            for var in filter_variables(p_obj, intent=VarIntent.IN).values():
                state_key, od_key = self._get_var_key(p_name, var)
                if state_key in in_dict:
                    in_dict[state_key].add(p_name)
                if od_key in in_dict:
                    in_dict[od_key].add(p_name)

        # filter out variables that do not need to be checked (without inputs):
        # inout_dict = {k: v for k, v in inout_dict.items() if k in in_dict}

        for key, inout_ps in inout_dict.items():
            in_ps = in_dict[key]

            verified_ios = []
            # now we only have to search and verify all inout variables
            for io_p in inout_ps:
                io_stack = [io_p]
                while io_stack:
                    cur = io_stack[-1]
                    if cur in verified_ios:
                        io_stack.pop()
                        continue

                    child_ios = self._deps_dict[cur].intersection(inout_ps - {cur})
                    if child_ios:
                        if child_ios == set(verified_ios):
                            child_ins = in_ps.intersection(self._deps_dict[cur])
                            # verify that all children have the previous io as
                            # dependency
                            problem_children = {}
                            for child_in in child_ins:
                                # we want to list all processes that should
                                # depend on the previous
                                # io-io
                                #    /
                                #  in
                                if not verified_ios[-1] in self._deps_dict[child_in]:
                                    problem_children[child_in] = [
                                        p
                                        for p in verified_ios
                                        if p not in self._deps_dict[child_in]
                                    ]
                            if problem_children:
                                raise RuntimeError(
                                    f"While checking {key}, {cur} updates it"
                                    f" and depends on some processes that use"
                                    f" it, but they do not depend on {verified_ios[-1]}"
                                    f" place them somewhere between or before "
                                    f"their values: {problem_children}"
                                )
                            # we can now safely remove these in nodes
                            in_ps -= child_ins
                            verified_ios.append(cur)
                            io_stack.pop()
                        elif child_ios - set(verified_ios):
                            io_stack.extend(child_ios)
                        else:
                            # the problem here is that
                            # io-..-io
                            #  \
                            #  io
                            problem_ios = [
                                p for p in verified_ios if p not in child_ios
                            ]
                            raise RuntimeError(
                                f"while checking {key}, order of inout process "
                                f"{cur} compared to {problem_ios} could not be "
                                f"established"
                            )
                    else:
                        # we are at the bottom inout process: remove in
                        # variables from the set
                        # this can only happen if we are the first process at
                        # the bottom
                        if verified_ios:
                            # the problem here is
                            # io->..->io
                            #         /
                            #        io
                            problem_ios = [
                                p for p in verified_ios if cur not in self._deps_dict[p]
                            ]
                            raise RuntimeError(
                                f"While checking {key}, inout process "
                                f"{verified_ios[-1]} has two branch dependencies."
                                f" Place {cur} before or somewhere between "
                                f"{verified_ios[:-1]}"
                            )
                        in_ps -= self._deps_dict[cur]
                        verified_ios.append(cur)
                        io_stack.pop()

            # we finished all inout, and inputs that are descendants of inout
            # vars, so all remaining input vars should depend on the last inout
            # var
            problem_ins = {}
            for p in in_ps:
                if not verified_ios[-1] in self._deps_dict[p]:
                    problem_ins[p] = [
                        prob for prob in verified_ios if prob not in self._deps_dict[p]
                    ]

            if problem_ins:
                raise RuntimeError(
                    f"while checking {key}, some input processes do not depend "
                    f"on {verified_ios[-1]}, with all inout processes {verified_ios}"
                    f" place them somewhere in between or before their values: {problem_ins}"
                )

    def get_sorted_processes(self):
        self._sorted_processes = OrderedDict(
            [(p_name, self._processes_obj[p_name]) for p_name in self._sort_processes()]
        )
        return self._sorted_processes


class Model(AttrMapping):
    """An immutable collection of process units that together form a
    computational model.

    This collection is ordered such that the computational flow is
    consistent with process inter-dependencies.

    Ordering doesn't always need to be explicitly provided ; it is dynamically
    computed using the processes interfaces. For other cases, custom
    dependencies can be supplied.

    Processes interfaces are also used for automatically retrieving
    the model inputs, i.e., all the variables that require setting a
    value before running the model.

    """

    active = []

    def __init__(self, processes, custom_dependencies={}, strict_order_check=False):
        """
        Parameters
        ----------
        processes : dict
            Dictionary with process names as keys and classes (decorated with
            :func:`process`) as values.
        custom_dependencies : dict
            Dictionary of custom dependencies.
            keys are process names and values iterable of process names that it
            depends on.
        strict_order_check : bool
            if True, aggresively check for correct ordering. (default: False)
            For a variable with processes for which it is an inout variable, it
            should look like: ``ins0->inout1->ins1->inout2->ins2``

        Raises
        ------
        :exc:`NotAProcessClassError`
            If values in ``processes`` are not classes decorated with
            :func:`process`.

        """
        builder = _ModelBuilder({k: get_process_cls(v) for k, v in processes.items()})

        builder.bind_processes(self)
        builder.set_process_keys()

        self._state = builder.set_state()
        self._var_cache = builder.create_variable_cache()

        self._all_vars = builder.get_variables()
        self._all_vars_dict = None

        self._index_vars = builder.get_variables(var_type=VarType.INDEX)
        self._index_vars_dict = None

        self._od_vars = builder.get_variables(var_type=VarType.ON_DEMAND)

        builder.ensure_no_intent_conflict()

        self._input_vars = builder.get_input_variables()
        self._input_vars_dict = None

        self._processes_to_validate = builder.get_processes_to_validate()

        # clean custom dependencies
        self._custom_dependencies = {}
        for p_name, c_deps in custom_dependencies.items():
            c_deps = {c_deps} if isinstance(c_deps, str) else set(c_deps)
            self._custom_dependencies[p_name] = c_deps

        self._dep_processes = builder.get_process_dependencies(
            self._custom_dependencies
        )
        self._processes = builder.get_sorted_processes()

        self._strict_order_check = strict_order_check
        if self._strict_order_check:
            builder._strict_order_check()

        super(Model, self).__init__(self._processes)
        self._initialized = True

    def _get_vars_dict_from_cache(self, attr_name):
        dict_attr_name = attr_name + "_dict"

        if getattr(self, dict_attr_name) is None:
            vars_d = defaultdict(list)

            for p_name, var_name in getattr(self, attr_name):
                vars_d[p_name].append(var_name)

            setattr(self, dict_attr_name, dict(vars_d))

        return getattr(self, dict_attr_name)

    @property
    def all_vars(self):
        """Returns all variables in the model as a list of
        ``(process_name, var_name)`` tuples (or an empty list).

        """
        return self._all_vars

    @property
    def all_vars_dict(self):
        """Returns all variables in the model as a dictionary of lists of
        variable names grouped by process.

        """
        return self._get_vars_dict_from_cache("_all_vars")

    @property
    def index_vars(self):
        """Returns all index variables in the model as a list of
        ``(process_name, var_name)`` tuples (or an empty list).

        """
        return self._index_vars

    @property
    def index_vars_dict(self):
        """Returns all index variables in the model as a dictionary of lists of
        variable names grouped by process.

        """
        return self._get_vars_dict_from_cache("_index_vars")

    @property
    def input_vars(self):
        """Returns all variables that require setting a value before running
        the model.

        A list of ``(process_name, var_name)`` tuples (or an empty list)
        is returned.

        """
        return self._input_vars

    @property
    def input_vars_dict(self):
        """Returns all variables that require setting a value before running
        the model.

        Unlike :attr:`Model.input_vars`, a dictionary of lists of
        variable names grouped by process is returned.

        """
        return self._get_vars_dict_from_cache("_input_vars")

    @property
    def dependent_processes(self):
        """Returns a dictionary where keys are process names and values are
        lists of the names of dependent processes.

        """
        return self._dep_processes

    def visualize(
        self,
        show_only_variable=None,
        show_variable_stages=None,
        show_inputs=False,
        show_variables=False,
        show_feedbacks=True,
    ):
        """Render the model as a graph using dot (require graphviz).

        Parameters
        ----------
        show_only_variable : tuple, optional
            Show only a variable (and all other variables sharing the
            same value) given as a tuple ``(process_name, variable_name)``.
            Deactivated by default.
        show_inputs : bool, optional
            If True, show all input variables in the graph (default: False).
            Ignored if `show_only_variable` is not None.
        show_variables : bool, optional
            If True, show also the other variables (default: False).
            Ignored if ``show_only_variable`` is not None.
        show_feedbacks: bool, optional
            if True, draws dotted arrows to indicate what processes use updated
            variables in the next timestep. (default: True)
            Ignored if `show_variables` is not None

        See Also
        --------
        :func:`dot.dot_graph`

        """
        from .dot import dot_graph

        return dot_graph(
            self,
            show_only_variable=show_only_variable,
            show_variable_stages=show_variable_stages,
            show_inputs=show_inputs,
            show_variables=show_variables,
            show_feedbacks=show_feedbacks,
        )

    @property
    def state(self):
        """Returns a mapping of model variables and their current value.

        Mapping keys are in the form of ``('process_name', 'var_name')`` tuples.

        This mapping does not include "on demand" variables.
        """
        return self._state

    def update_state(
        self, input_vars, validate=True, ignore_static=False, ignore_invalid_keys=True
    ):
        """Update the model's state (only input variables) with new values.

        Prior to update the model's state, first convert the values for model
        variables that have a converter, otherwise copy the values.

        Parameters
        ----------
        input_vars : dict_like
            A mapping where keys are in the form of
            ``('process_name', 'var_name')`` tuples and values are
            the input values to set in the model state.
        validate : bool, optional
            If True (default), run the variable validators after setting the
            new values.
        ignore_static : bool, optional
            If True, sets the values even for static variables. Otherwise
            (default), raises a ``ValueError`` in order to prevent updating
            values of static variables.
        ignore_invalid_keys : bool, optional
            If True (default), ignores keys in ``input_vars`` that do not
            correspond to input variables in the model. Otherwise, raises
            a ``KeyError``.

        """
        for key, value in input_vars.items():

            if key not in self.input_vars:
                if ignore_invalid_keys:
                    continue
                else:
                    raise KeyError(f"{key} is not a valid input variable in model")

            var = self._var_cache[key]["attrib"]

            if not ignore_static and var.metadata.get("static", False):
                raise ValueError(f"Cannot set value for static variable {key}")

            if var.converter is not None:
                self._state[key] = var.converter(value)
            else:
                self._state[key] = copy.copy(value)

        if validate:
            p_names = set([pn for pn, _ in input_vars if pn in self._processes])
            self.validate(p_names)

    @property
    def cache(self):
        """Returns a mapping of model variables and some of their (meta)data cached for
        fastpath access.

        Mapping keys are in the form of ``('process_name', 'var_name')`` tuples.

        """
        return self._var_cache

    def update_cache(self, var_key):
        """Update the model's cache for a given model variable.

        This is generally not really needed, except for on demand variables
        where this might optimize multiple accesses to the variable value between
        two simulation stages.

        No copy is performed.

        Parameters
        ----------
        var_key : tuple
            Variable key in the form of a ``('process_name', 'var_name')``
            tuple.

        """
        p_name, v_name = var_key
        self._var_cache[var_key]["value"] = getattr(self._processes[p_name], v_name)

    def validate(self, p_names=None):
        """Run the variable validators of all or some of the processes
        in the model.

        Parameters
        ----------
        p_names : list, optional
            Names of the processes to validate. If None is given (default),
            validators are run for all processes.

        """
        if p_names is None:
            processes = self._processes.values()
        else:
            processes = [self._processes[pn] for pn in p_names]

        for p_obj in processes:
            attr.validate(p_obj)

    def _call_hooks(self, hooks, runtime_context, stage, level, trigger):
        try:
            event_hooks = hooks[stage][level][trigger]
        except KeyError:
            return RuntimeSignal.NONE

        signals = []

        for h in event_hooks:
            s = h(self, Frozen(runtime_context), Frozen(self.state))

            if s is None:
                s = RuntimeSignal(0)
            else:
                s = RuntimeSignal(s)

            signals.append(s)

        # Signal with highest value has highest priority
        return RuntimeSignal(max([s.value for s in signals]))

    def _execute_process(
        self, p_obj, stage, runtime_context, hooks, validate, state=None
    ):
        """Internal process execution method, which calls the process object's
        executor.

        A state may be passed to the executor instead of using the executor's
        state (this is to avoid stateful objects when calling the executor
        during execution of a Dask graph).

        The process executor returns a partial state (only the variables that
        have been updated by the executor, which will be needed for executing
        further tasks in the Dask graph).

        This method returns this updated state as well as any runtime signal returned
        by the hook functions and/or the executor (the one with highest priority).

        """
        executor = p_obj.__xsimlab_executor__
        p_name = p_obj.__xsimlab_name__

        signal_pre = self._call_hooks(hooks, runtime_context, stage, "process", "pre")

        if signal_pre.value > 0:
            return p_name, ({}, signal_pre)

        state_out, signal_out = executor.execute(
            p_obj, stage, runtime_context, state=state
        )

        signal_post = self._call_hooks(hooks, runtime_context, stage, "process", "post")
        if signal_post.value > signal_out.value:
            signal_out = signal_post

        if validate:
            self.validate(self._processes_to_validate[p_name])

        return p_name, (state_out, signal_out)

    def _build_dask_graph(self, execute_args):
        """Build a custom, 'stateless' graph of tasks (process execution) that will
        be passed to a Dask scheduler.

        """

        def exec_process(p_obj, model_state, exec_outputs):
            # update model state with output states from all dependent processes
            # gather signals returned by all dependent processes and sort them by highest priority
            state = {}
            signal = RuntimeSignal.NONE

            state.update(model_state)

            for _, (state_out, signal_out) in exec_outputs:
                state.update(state_out)

                if signal_out.value > signal.value:
                    signal = signal_out

            if signal == RuntimeSignal.BREAK:
                # received a BREAK signal from the execution of a dependent process
                # -> skip execution of current process as well as all downstream processes
                #    in the graph (by forwarding the signal).
                return p_obj.__xsimlab_name__, ({}, signal)
            else:
                return self._execute_process(p_obj, *execute_args, state=state)

        dsk = {}
        for p_name, p_deps in self._dep_processes.items():
            dsk[p_name] = (exec_process, self._processes[p_name], self._state, p_deps)

        # add a node to gather output signals and state from all executed processes
        dsk["_gather"] = (
            lambda exec_outputs: dict(exec_outputs),
            list(self._processes),
        )

        return dsk

    def _merge_exec_outputs(self, exec_outputs) -> RuntimeSignal:
        """Collect and merge process execution outputs (from dask graph).

        - combine all output states and update model's state.
        - sort all output runtime signals and return the signal with highest priority.

        """
        new_state = {}
        signal = RuntimeSignal.NONE

        # process order matters for properly updating state!
        for p_name in self._processes:
            state_out, signal_out = exec_outputs[p_name]
            new_state.update(state_out)
            if signal_out.value > signal.value:
                signal = signal_out

        self._state.update(new_state)

        # need to re-assign the updated state to all processes
        # for access between simulation stages (e.g., save snapshots)
        for p_obj in self._processes.values():
            p_obj.__xsimlab_state__ = self._state

        return signal

    def _clear_od_cache(self):
        """Clear cached values of on-demand variables."""

        for key in self._od_vars:
            self._state.pop(key, None)

    def execute(
        self,
        stage,
        runtime_context,
        hooks=None,
        validate=False,
        parallel=False,
        scheduler=None,
    ):
        """Run one stage of a simulation.

        Parameters
        ----------
        stage : {'initialize', 'run_step', 'finalize_step', 'finalize'}
            Name of the simulation stage.
        runtime_context : dict
            Dictionary containing runtime variables (e.g., time step
            duration, current step).
        hooks : dict, optional
            Runtime hook callables, grouped by simulation stage, level and
            trigger pre/post.
        validate : bool, optional
            If True, run the variable validators in the corresponding
            processes after a process (maybe) sets values through its foreign
            variables (default: False). This is useful for debugging but
            it may significantly impact performance.
        parallel : bool, optional
            If True, run the simulation stage in parallel using Dask
            (default: False).
        scheduler : str, optional
            Dask's scheduler used to run the stage in parallel
            (Dask's threads scheduler is used as failback).

        Returns
        -------
        signal : :class:`RuntimeSignal`
            Signal with hightest priority among all signals returned by hook
            functions and/or process runtime methods, if any. Otherwise,
            returns ``RuntimeSignal.NONE``.

        Notes
        -----
        Even when run in parallel, xarray-simlab ensures that processes will
        not be executed before their dependent processes. However, race
        conditions or perfomance issues may still occur under certain
        circumstances that require extra care. In particular:

        - The gain in perfomance when running the processes in parallel
          highly depends on the graph structure. It might not be worth the
          extra complexity and overhead.
        - If a multi-threaded scheduler is used, then the code implemented
          in the process classes must be thread-safe. Also, it should release
          the Python Global Interpreted Lock (GIL) as much as possible in order
          to see a gain in performance.
        - Multi-process or distributed schedulers may have very poor performance,
          especially when a lot of data (model state) is shared between the model
          processes. The way xarray-simlab scatters/gathers this data between the
          scheduler and the workers is not optimized at all. Addtionally, those
          schedulers may not work well with the given ``hooks`` and/or when the
          processes runtime methods rely on instance attributes that are not
          explicitly declared as model variables.

        """
        # TODO: issue warning if validate is True and "processes" or distributed scheduler
        # is used (not supported)

        if hooks is None:
            hooks = {}

        stage = SimulationStage(stage)
        execute_args = (stage, runtime_context, hooks, validate)

        self._clear_od_cache()

        signal_pre = self._call_hooks(hooks, runtime_context, stage, "model", "pre")

        if signal_pre.value > 0:
            return signal_pre

        if parallel:
            dsk_get = dask.base.get_scheduler(scheduler=scheduler)
            if dsk_get is None:
                dsk_get = dask.threaded.get

            dsk = self._build_dask_graph(execute_args)
            exec_outputs = dsk_get(dsk, "_gather", scheduler=scheduler)

            # TODO: without this -> flaky tests (don't know why)
            # state is not properly updated -> error when writing output vars in store
            if isinstance(scheduler, Client):
                time.sleep(0.001)

            signal_process = self._merge_exec_outputs(exec_outputs)

        else:
            for p_obj in self._processes.values():
                _, (_, signal_process) = self._execute_process(p_obj, *execute_args)

                if signal_process == RuntimeSignal.BREAK:
                    break

        signal_post = self._call_hooks(hooks, runtime_context, stage, "model", "post")

        return signal_post

    def clone(self):
        """Clone the Model.

        Returns
        -------
        cloned : Model
            New Model instance with the same processes.

        """
        processes_cls = {k: type(obj) for k, obj in self._processes.items()}
        return type(self)(processes_cls)

    def update_processes(self, processes):
        """Add or replace processe(s) in this model.

        Parameters
        ----------
        processes : dict
            Dictionnary with process names as keys and process classes
            as values.

        Returns
        -------
        updated : Model
            New Model instance with updated processes.

        """
        processes_cls = {k: type(obj) for k, obj in self._processes.items()}
        processes_cls.update(processes)
        return type(self)(processes_cls)

    def drop_processes(self, keys):
        """Drop processe(s) from this model.

        Parameters
        ----------
        keys : str or iterable of str
            Name(s) of the processes to drop.

        Returns
        -------
        dropped : Model
            New Model instance with dropped processes.

        """
        keys = {keys} if isinstance(keys, str) else set(keys)

        processes_cls = {
            k: type(obj) for k, obj in self._processes.items() if k not in keys
        }

        # we also should check for chains of deps e.g.
        # a->b->c->d->e where {b,c,d} are removed
        # then we have a->e left over.
        # perform a depth-first search on custom dependencies
        # and let the custom deps propagate forward
        completed = set()
        for key in self._custom_dependencies:
            if key in completed:
                continue
            key_stack = [key]
            while key_stack:
                cur = key_stack[-1]
                if cur in completed:
                    key_stack.pop()
                    continue

                # if we have custom dependencies that are removed
                # and are fully traversed, add their deps to the current
                child_keys = keys.intersection(self._custom_dependencies[cur])
                if child_keys.issubset(completed):
                    # all children are added, so we are safe
                    self._custom_dependencies[cur].update(
                        *[
                            self._custom_dependencies[child_key]
                            for child_key in child_keys
                        ]
                    )
                    self._custom_dependencies[cur] -= child_keys
                    completed.add(cur)
                    key_stack.pop()
                else:  # if child_keys - completed:
                    # we need to search deeper: add to the stack.
                    key_stack.extend([k for k in child_keys - completed])

        # now also remove keys from custom deps
        for key in keys:
            if key in self._custom_dependencies:
                del self._custom_dependencies[key]

        return type(self)(processes_cls, self._custom_dependencies)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return NotImplemented

        for (k1, v1), (k2, v2) in zip(
            self._processes.items(), other._processes.items()
        ):
            if k1 != k2 or type(v1) is not type(v2):
                return False

        return True

    def __enter__(self):
        if len(Model.active):
            raise ValueError("There is already a model object in context")

        Model.active.append(self)
        return self

    def __exit__(self, *args):
        Model.active.pop(0)

    def __repr__(self):
        return repr_model(self)
