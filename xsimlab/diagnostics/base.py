import inspect
from typing import Callable, Dict, Iterable, List, Union

from ..process import SimulationStage


__all__ = ("runtime_hook", "RuntimeDiagnostics")


def runtime_hook(stage, level="model", trigger="post"):
    """Decorator that allows to call a function or a method
    at one or more specific times during a simulation.

    The decorated function / method must have the following signature:
    ``func(model, context, state)`` or ``meth(self, model, context, state)``.

    Parameters
    ----------
    stage : {'initialize', 'run_step', 'finalize_step', 'finalize'}
        The simulation stage at which to call the function.
    level : {'model', 'process'}
        Sets whether the simulation stage is treated model-wise ('model')
        or process-wise ('process'). In the model-wise case (default), the
        function is called only once during the execution of the simulation
        stage. In the process-wise case, the function is executed as many
        times as there are processes in the model that provide an
        implementation of that simulation stage.
    trigger : {'pre', 'post'}
        Sets when exactly to trigger the function call, i.e., just before
        ('pre') or just after ('post') the execution of the model's or
        process' simulation stage (default: after).

    """
    stage = SimulationStage(stage)

    if level not in ("model", "process"):
        raise ValueError("level argument must be either 'model' or 'process'")

    if trigger not in ("pre", "post"):
        raise ValueError("trigger argument must be either 'pre' or 'post'")

    def wrap(func):
        func.__xsimlab_hook__ = (stage, level, trigger)
        return func

    return wrap


def _get_hook_info(func):
    return getattr(func, "__xsimlab_hook__", False)


def group_hooks(
    hooks: Iterable[Callable],
) -> Dict[SimulationStage, Dict[str, Dict[str, List[Callable]]]]:
    """Group a flat sequence of runtime hook functions by
    simulation stage -> level -> trigger (pre/post)

    """
    grouped = {}

    for h in hooks:
        stage, level, trigger = h.__xsimlab_hook__

        if stage not in grouped:
            grouped[stage] = {}
        if level not in grouped[stage]:
            grouped[stage][level] = {}
        if trigger not in grouped[stage][level]:
            grouped[stage][level][trigger] = []

        grouped[stage][level][trigger].append(h)

    return grouped


class RuntimeDiagnostics:
    """Base class for simulation runtime diagnostics.

    Create some runtime hook functions

    >>> @runtime_hook('initialize', 'model', 'pre')
    ... def start(model, context, state):
    ...     pass

    >>> @runtime_hook('run_step', 'model', 'post')
    ... def after_step(model, context, state):
    ...     pass

    You may then create a ``RuntimeDiagnostics`` object with any number
    of runtime hooks

    >>> rd = RuntimeDiagnostics(hooks=[start, after_step])

    And use it either as a context manager over a model run call

    >>> with rd:
    ...    in_dataset.xsimlab.run(model=model)

    Or globally with the ``register`` method

    >>> rd.register()
    >>> rd.unregister()

    Alternatively subclass ``RuntimeDiagnostics`` with some runtime hook
    methods

    >>> class PrintStep(RuntimeDiagnostics):
    ...     @runtime_hook('run_step', 'model', 'pre')
    ...     def before_step(self, model, context, state):
    ...         print(f"starting step {context['step']}")

    >>> with PrintStep():
    ...     in_dataset.xsimlab.run(model=model)

    """

    active = set()

    def __init__(self, *args):
        """
        Parameters
        ----------
        *args : callable
            An abitrary number of runtime_hook decorated functions.

        See Also
        --------
        :func:`runtime_hook`

        """
        if not all(_get_hook_info(h) for h in args):
            raise TypeError("Arguments must be only runtime_hook decorated functions")

        self._hook_args = args

    def _get_hooks(self):
        hook_methods = [
            m for _, m in inspect.getmembers(self, predicate=_get_hook_info)
        ]

        return getattr(self, "_hook_args", ()) + tuple(hook_methods)

    def register(self):
        """Globally register this instance of runtime diagnostics."""
        RuntimeDiagnostics.active.add(self)

    def unregister(self):
        """Globally unresgister this instance of runtime diagnostics."""
        RuntimeDiagnostics.active.remove(self)

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, typ, value, traceback):
        self.unregister()


def flatten_diagnostics(
    objects: Iterable[Union[RuntimeDiagnostics, Callable]]
) -> List[Callable]:
    """Return a list of runtime hook functions from a sequence of
    runtime hook functions or RuntimeDiagnostics objects.

    """
    hooks = []

    for obj in objects:
        if isinstance(obj, RuntimeDiagnostics):
            hooks += list(obj._get_hooks())
        elif _get_hook_info(obj):
            hooks.append(obj)
        else:
            raise TypeError(
                "{obj!r} is not a RuntimeDiagnostics object nor a runtime hook decorated function"
            )

    return hooks