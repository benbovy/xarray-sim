.. _create_model:

Create and Modify Models
========================

Like the previous Section :doc:`framework`, this section is useful
mostly for users who want to create new models from scratch or
customize existing models. Users who only want to run simulations from
existing models may skip this section.

As a simple example, we will start here from a model which numerically
solves the 1-d advection equation using the `Lax method`_. The equation
may be written as:

.. math::

   \frac{\partial u}{\partial t} + \nu \frac{\partial u}{\partial x} = 0

with :math:`u(x, t)` as the quantity of interest and where :math:`\nu`
is the velocity. The discretized form of this equation may be written
as:

.. math::

   u^{n+1}_i = \frac{1}{2} (u^n_{i+1} + u^n_{i-1}) - \nu \frac{\Delta t}{2 \Delta x} (u^n_{i+1} - u^n_{i-1})

Where :math:`\Delta x` is the fixed spacing between the nodes
:math:`i` of a uniform grid and :math:`\Delta t` is the step duration
between times :math:`n` and :math:`n+1`.

We could just implement this numerical model with a few lines of
Python / Numpy code, e.g., here below assuming periodic boundary
conditions and a Gaussian pulse as initial profile. We will show,
however, that it is very easy to refactor this code for using it with
xarray-simlab. We will also show that, while enabling useful features,
the refactoring still results in a short amount of readable code.

.. literalinclude:: scripts/advection_model_numpy.py

.. _`Lax method`: https://en.wikipedia.org/wiki/Lax%E2%80%93Friedrichs_method

Anatomy of a Process subclass
-----------------------------

Let's first wrap the code above into a single class named
``AdvectionLax1D`` decorated by :class:`~xsimlab.process`. Next we'll
explain in detail the content of this class.

.. literalinclude:: scripts/advection_model.py
   :lines: 3-34

Process interface
~~~~~~~~~~~~~~~~~

``AdvectionLax1D`` has some class attributes declared at the top,
which together form the process' "public" interface, i.e., all the
variables that we want to be publicly exposed by this process. Here we
use :func:`~xsimlab.variable` to add some metadata to each variable
of the interface.

We first may specify the labels of the dimensions expected for each
variable, which defaults to an empty tuple (i.e., a scalar value is
expected). In this example, variables ``spacing``, ``length``, ``loc``
and ``scale`` are all scalars, whereas ``x`` and ``u`` are both arrays
defined on the 1-dimensional :math:`x` grid. Multiple choices can also
be given as a list, like variable ``v`` which represents a velocity
field that can be either constant (scalar) or variable (array) in
space.

Additionally, it is also possible to add a short ``description``
and/or custom metadata like units with the ``attrs`` argument.

Another important argument is ``intent``, which specifies how the
process deals with the value of the variable. By default,
``intent='in'`` means that the process needs a value set for the
variable for its computation ; this value should either be computed
elsewhere by another process or be provided by the user as model
input. By contrast, variables ``x`` and ``u`` have ``intent='out'``,
which means that the process ``AdvectionLax1D`` itself initializes and
computes a value for these two variables.

Note also ``static=True`` set for ``spacing``, ``length``, ``loc`` and
``scale``. This is to prevent providing time varying values as model inputs for
those parameters. By default, it is possible to change the value of a variable
during a simulation (external forcing), see Section :ref:`time_varying_inputs`
for an example. This is not always desirable, though.

Process "runtime" methods
~~~~~~~~~~~~~~~~~~~~~~~~~

Beside its interface, the process ``AdvectionLax1D`` also implements
methods that will be called during simulation runtime:

- ``.initialize()`` will be called once at the beginning of a
  simulation. Here it is used to set the x-coordinate values of the
  grid and the initial values of ``u`` along the grid (Gaussian
  pulse).
- ``.run_step()`` will be called at each time step iteration. This is
  where the Lax method is implemented.
- ``.finalize_step()`` will be called at each time step iteration too
  but after having called ``run_step`` for all other processes (if
  any). Its intended use is mainly to ensure that state variables like
  ``u`` are updated consistently and after having taken snapshots.

A fourth method ``.finalize()`` could also be implemented, but it is
not needed in this case. This method is called once at the end of the
simulation, e.g., for some clean-up.

Each of these methods can be decorated with :func:`~xsimlab.runtime`
to pass some useful information during simulation runtime (e.g.,
current time step number, current time or time step duration), which
may be needed for the computation. Without this decorator, runtime
methods must have no other parameter than ``self``.

Getting / setting variable values
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For each variable declared as class attributes in ``AdvectionLax1D``
we can get their value (and/or set a value depending on their
``intent``) elsewhere in the class like if it was defined as regular
instance attributes, e.g., using ``self.u`` for variable ``u``.

.. note::

   In xarray-simlab it is safe to run multiple simulations
   concurrently: each simulation has its own process instances.

Beside variables declared in the process interface, nothing prevent us
from using regular attributes in process classes if needed. For
example, ``self.u1`` is set as a temporary internal state in
``AdvectionLax1D`` to wait for the "finalize step" stage before
updating :math:`u`.

Creating a Model instance
-------------------------

Creating a new :class:`~xsimlab.Model` instance is very easy. We just
need to provide a dictionary with the process class(es) that we want
to include in the model, e.g., with only the process created above:

.. literalinclude:: scripts/advection_model.py
   :lines: 37

That's it! Now we have different tools already available to inspect
the model (see Section :doc:`inspect_model`). We can also use that
model with the xarray extension provided by xarray-simlab to create
new setups, run the model, take snapshots for one or more variables on
a given frequency, etc. (see Section :doc:`run_model`).

Fine-grained process refactoring
--------------------------------

The model created above isn't very flexible. What if we want to change
the initial conditions? Use a grid with variable spacing? Add another
physical process impacting :math:`u` such as a source or sink term?
In all cases we would need to modify the class ``AdvectionLax1D``.

This framework works best if we instead split the problem into small
pieces, i.e., small process classes that we can easily combine and
replace in models.

The ``AdvectionLax1D`` process may for example be refactored into 4
separate processes:

- ``UniformGrid1D`` : grid creation
- ``ProfileU`` : update :math:`u` values along the grid at each time
  iteration
- ``AdvectionLax`` : perform advection at each time iteration
- ``InitUGauss`` : create initial :math:`u` values along the grid.

**UniformGrid1D**

This process declares all grid-related variables and computes
x-coordinate values.

.. literalinclude:: scripts/advection_model.py
   :lines: 40-49

All grid variables are static, i.e., their values must be time-invariant. The
``x`` variable is declared using :func:`~xsimlab.index`. This is a specific kind
of variable intended for storing coordinate labels, here useful for indexing any
data on the grid. ``x`` values must be set somewhere in the process runtime
methods and they should also be time-invariant (i.e., all index variables imply
``intent='out'`` and ``static=True``). Those values are set here once at the
beginning of the simulation ; there is no need to implement ``.run_step()``.

**ProfileU**

.. literalinclude:: scripts/advection_model.py
   :lines: 52-65

``u_vars`` is declared as a :func:`~xsimlab.group` variable, i.e., an
iterable of all variables declared elsewhere that belong the same
group ('u_vars' in this case). In this example, it allows to further
add one or more processes that will also affect the evolution of
:math:`u` in addition to advection (see below).

Note also ``intent='inout'`` set for ``u``, which means that
``ProfileU`` updates the value of :math:`u` but still needs an initial
value from elsewhere.

**AdvectionLax**

.. literalinclude:: scripts/advection_model.py
   :lines: 68-88

``u_advected`` represents the effect of advection on the evolution of
:math:`u` and therefore belongs to the group 'u_vars'.

Computing values of ``u_advected`` requires values of variables
``spacing`` and ``u`` that are already declared in the
``UniformGrid1D`` and ``ProfileU`` classes, respectively.  Here we
declare them as :func:`~xsimlab.foreign` variables, which allows to
handle them like if these were the original variables. For example,
``self.grid_spacing`` in this class will return the same value than
``self.spacing`` in ``UniformGrid1D``.

**InitUGauss**

.. literalinclude:: scripts/advection_model.py
   :lines: 91-101

A foreign variable can also be used to set values for variables that
are declared in other processes, as for ``u`` here with
``intent='out'``.

**Refactored model**

We now have all the building blocks to create a more flexible model:

.. literalinclude:: scripts/advection_model.py
   :lines: 104-111

The order in which the processes are given in the dictionary doesn't matter.
When creating a new instance of :class:`~xsimlab.Model`, the xarray-simlab
modeling framework automatically sorts the given processes into a
computationally consistent order and retrieves the model inputs among all
declared variables in all processes.

In terms of computation and inputs, ``advect_model`` is equivalent to the
``advect_model_raw`` instance created above ; it is just organized
differently.

Update existing models
----------------------

Between the two Model instances created so far, the advantage of
``advect_model`` over ``advect_model_raw`` is that we can easily update the model --
change its behavior and/or add many new features -- without
sacrificing readability or losing the ability to get back to the
original, simple version.

**Example: adding a source term at a specific location**

For this we create a new process:

.. literalinclude:: scripts/advection_model.py
   :lines: 114-141

Some comments about this class:

- ``u_source`` belongs to the group 'u_vars' and therefore will be
  added to ``u_advected`` in ``ProfileU`` process.
- Methods and/or properties other than the reserved "runtime" methods
  may be added in a Process subclass, just like in any other Python
  class.
- Nearest node index and source rate array will be recomputed at each
  time iteration because variables ``loc`` and ``flux`` may both have
  a time dimension (variable source location and intensity), i.e.,
  ``self.loc`` and ``self.flux`` may both change at each
  time iteration.

In this example we also want to start with a flat, zero :math:`u`
profile instead of a Gaussian pulse. We create another (minimal)
process for that:

.. literalinclude:: scripts/advection_model.py
   :lines: 144-152

Using one command, we can then update the model with these new
features:

.. literalinclude:: scripts/advection_model.py
   :lines: 155-157

Compared to ``advect_model``, this new ``advect_model_src`` have a new process named
'source' and a replaced process 'init'.

**Removing one or more processes**

It is also possible to create new models by removing one or more
processes from existing Model instances, e.g.,

.. literalinclude:: scripts/advection_model.py
   :lines: 160

In this latter case, users will have to provide initial values of
:math:`u` along the grid directly as an input array.

.. note::

   Model instances are immutable, i.e., once created it is not
   possible to modify these instances by adding, updating or removing
   processes. Both methods ``.update_processes()`` and
   ``.drop_processes()`` always return new instances of ``Model``.

Customize existing processes
----------------------------

Sometimes we only want to update an existing model with very minor
changes.

As an example, let's update ``advect_model`` by using a fixed grid (i.e.,
with hard-coded values for grid spacing and length). One way to
achieve this is to create a small new process class that sets
the values of ``spacing`` and ``length``:

.. literalinclude:: scripts/advection_model.py
   :lines: 163-170

However, one drawback of this "additive" approach is that the number
of processes in a model might become unnecessarily high:

.. literalinclude:: scripts/advection_model.py
   :lines: 173-175

Alternatively, it is possible to write a process class that inherits
from ``UniformGrid1D``, in which we can re-declare variables *and/or*
re-define "runtime" methods:

.. literalinclude:: scripts/advection_model.py
   :lines: 178-186

We can here directly update the model and replace the original process
``UniformGrid1D`` by the inherited class ``FixedGrid``. Foreign
variables that refer to ``UniformGrid1D`` will still correctly point
to the ``grid`` process in the updated model:

.. literalinclude:: scripts/advection_model.py
   :lines: 189

.. warning::

   This feature is experimental! It may be removed in a next version of
   xarray-simlab.

   In particular, linking foreign variables in a model is ambiguous
   when both conditions below are met:

   - two different processes in a model inherit from a common class
     (except ``object``)

   - a third process in the same model has a foreign variable that
     links to that common class

Process ordering and dependencies
---------------------------------
The execution order is uniquely determined based on process ordering, which is
derived from the intent of variables in different processes. However, this
ordering is only based on variables that have ``out`` intent. For processes that
update a variable, (``inout`` intent), the order is not automatically
established, and this has to be done using custom dependencies. For example, the
order in the following model is automatically determined:

.. ipython:: python
   :suppress:

   import sys
   sys.path.append('scripts')
   from xsimlab.dot import dot_graph
   from process_ordering import *

.. literalinclude:: scripts/process_ordering.py
   :lines: 4-7,14-16

.. ipython:: python

   model = xs.Model({'out':Out1,'inout':Inout1})

.. ipython:: python
   :suppress:

   dot_graph(model,filename='savefig/ordering_out_inout.png')

.. image:: savefig/ordering_out_inout.png

The order would have been the same when `inout` had had ``intent=in``
for ``var``. Here, the ``out`` variable is always calculated first. Both processes
can change the value for ``var``. However, the ``out`` process will only use the
calculated value from ``inout`` in the next timestep. In this way, feedback
loops can be implemented in xarray-simlab.

**More on ordering**

For a complete understanding of in which order processes are executed, take the
following example:

.. ipython:: python
   :suppress:

   model=xs.Model({'A':Other,'B':In2,'C':Inout1,'D':In3,'E':In1,'F':Inout2},
                  custom_dependencies={'F':{'D','E'},'E':{'C','A'},'D':'C','C':'B'})
   dot_graph(model,filename='savefig/ordering_complex.png')
   model=xs.Model({'A':Other,'B':In2,'C':Inout1,'D':In3,'E':In1,'F':Inout2},
                  custom_dependencies={'F':{'D','E'},'E':'C','D':'C','C':{'B','A'}})
   dot_graph(model,filename='savefig/ordering_simple.png')

.. image:: savefig/ordering_simple.png

Here, processes ``A`` and ``B`` are executed first. When both are finished,
``C`` can be executed, after which ``D`` and ``E`` can be executed. Finally,
when both are finished, ``F`` is executed. The mutual order of ``A`` and ``B``,
or ``D`` and ``E`` depends on how the graph is traversed, and cannot be
established beforehand.

Establishing an order can become even more complex, as in the following example:

.. image:: savefig/ordering_complex.png

Here, ``A`` can be calculated before or after ``B``, ``C``, or even ``D``, 
depending on how the graph is traversed. In the case of parallel processing, all
processes that can be calculated simultaneously are calculated simultaneously 
by Dask.

.. note::
   The order described above is executed on every runtime stage, so separately
   for :func:`initialize`, :func:`run_step`, :func:`finalize_step` (to be
   deprecated) and :func:`finalize`.
   

Custom dependencies
~~~~~~~~~~~~~~~~~~~
It may be necessary to add custom dependencies to a model. For example, when a
process that updates a variable (``inout``) is used by another process (``in``
or ``inout``), but there is no ordering determined in the model based on other
variables. 

.. literalinclude:: scripts/process_ordering.py
   :lines: 4-11

.. ipython:: python

   model = xs.Model({'inout':Inout1,'in':In1})

.. ipython:: python
   :suppress:

   dot_graph(model, filename='savefig/ordering_in_inout_noedge.png')

.. image:: savefig/ordering_in_inout_noedge.png

Now, there is no way to determine whether ``in`` uses the variable
``var`` before or after ``inout`` has modified it. In this case, the user can 
add custom dependencies:

.. ipython:: python

   model= xs.Model({'inout':Inout1,'in':In1},custom_dependencies={'inout':'in'})

.. ipython:: python
   :suppress:

   dot_graph(model, filename='savefig/ordering_in_inout.png')

.. image:: savefig/ordering_in_inout.png

Of course, this could also have been done the other way around, if ``in`` should
use the updated value of ``inout``.

It is also possible to provide a list of dependent processes as follows:

.. ipython:: python

   xs.Model({'inout':Inout1,'in1':In1,'in2':In2},
             custom_dependencies={'inout':['in1','in2']})

Internally, this is converted to a Python ``set``

**Dropping processes**

When dropping processes, *custom* dependencies of dropped processes are
automatically added to processes that explicitly depend on them. This also works
for longer chains of dropped processes and branched dependencies:

.. ipython:: python

   model = xs.Model({'in1':In1,'inout1':Inout1,'inout2':Inout2},
                     custom_dependencies={'in1':'inout2','inout2':'inout1'})
   model_dropped = model.drop_processes('inout2')

.. ipython:: python
   :suppress:

   dot_graph(model,filename='savefig/ordering_drop_notyet.png')
   dot_graph(model_dropped,filename='savefig/ordering_drop_processes.png')

.. image:: savefig/ordering_drop_notyet.png

becomes:

.. image:: savefig/ordering_drop_processes.png

.. _create_model_strict:

Strict checking
~~~~~~~~~~~~~~~
In a model with many different processes and even more variables, the 
abovementioned integrity may not be easy to verify. Therefore, a strict checking
algorithm has been provided. It both checks if all processes that update a
variable (``inout``) are in a linear order, and all processes that use it
(``in``) are strictly in between two ``inout`` processes.

It can be enabled at model creation as:

.. ipython:: python

   xs.Model({'in':In1,'inout':Inout1},
            custom_dependencies={'in':'inout'},
            strict_order_check=True)

and raises an error whenever a strict order cannot be determined. When multiple 
processes update a variable, they should be in a linear order. 
Furthermore, all processes that use that variable (``in``) should either be
before all ``inout`` processes, strictly in between two ``inout`` processes, or
after the last ``inout`` process. Additional interdependencies are also allowed.

**Checking processes that update a variable**

.. ipython:: python
   :suppress:

   model = xs.Model({'inout1':Inout1,'inout2':Inout2,'inout3':Inout3},
                    custom_dependencies={'inout2':'inout1','inout3':'inout1'},
                    strict_order_check=False)
   dot_graph(model,filename='savefig/strict_io_branch.png')
      model = xs.Model({'inout1':Inout1,'inout2':Inout2,'inout3':Inout3},
                    custom_dependencies={'inout3':{'inout1','inout2'}},
                    strict_order_check=False)
   dot_graph(model,filename='savefig/strict_branch_io.png')

.. image:: savefig/strict_branch_io.png

Here, the algorithm will find that either ``inout1`` or ``inout2`` does not have
dependent processes that update the variable, while another such process has
already been traversed. This can be solved by adding ``inout1->inout2`` or
``inout2->inout1`` to custom dependencies.

.. image:: savefig/strict_io_branch.png

Here, it will find that either ``inout2`` or ``inout3`` has ``inout1`` as *only*
dependent ``inout`` process for this variable, while both others have already
been traversed. Adding ``inout2->inout3`` or ``inout3->inout2`` solves this.

**Checking processes that use the variable**

When an ``in`` variable is to be used in between two processes that update it,
it should both depend on the first ``inout`` process, and the second ``inout``
process should also depend on it:

.. ipython:: python

   model = xs.Model({'inout1':Inout1,'inout2':Inout2,'in1':In1},
                     custom_dependencies={'inout2':['in1','inout1'],'in1':'inout1'})

.. ipython:: python
   :suppress:
   
   dot_graph(model, filename='savefig/strict_io_in_io.png')

.. image:: savefig/strict_io_in_io.png

Note that the ``'inout2':'inout1'`` dependency is actually redundant, since it
derives from ``'inout2':'in1'`` and ``'in1':'inout1'``.

.. ipython:: python
   :suppress:

   model = xs.Model({'inout1':Inout1,'inout2':Inout2,'in1':In1},
                    custom_dependencies={'inout2':{'inout1','in1'}})
   dot_graph(model,filename='savefig/strict_ioinio_branch.png')
   model = xs.Model({'inout1':Inout1,'inout2':Inout2,'in1':In1},
                    custom_dependencies={'inout2':'inout1','in1':'inout1'})
   dot_graph(model,filename='savefig/strict_branch_ioinio.png')

.. image:: savefig/strict_ioinio_branch.png

Here, ``inout2`` will be found to depend on ``in1``, but ``in1`` not on
``inout1``. ``in1`` can still be placed either before ``inout1->in1`` or after
``in1->inout1``.

.. image:: savefig/strict_branch_ioinio.png

Now, in the final check, it is found that ``in1`` does not depend on ``inout2``.
It can still be placed before ``in1->inout2`` or after ``inout2->in1``.

A full correct example with multiple redundant edges can be:

.. ipython:: python
   :suppress:

   model = xs.Model({'in1':In1,'other':Other,'inout1':Inout1,'in2':In2,'inout2':Inout2,'in3':In3},
                    custom_dependencies={
                                         'in3':['inout2','in2',
                                         'inout1','in1'],
                                         'inout2':['in2','in1'],
                                         'in2':['inout1','in1'],
                                         'inout1':'other',
                                         'other':'in1'})
   dot_graph(model, filename='savefig/strict_mayhem.png')
   
.. image:: savefig/strict_mayhem.png

Note that there is still a strict order that can be determined. Also, the
``other`` process makes that ``inout1`` does not directly depend on ``in1``.
This is fine, however, since ``in1`` is still executed first.