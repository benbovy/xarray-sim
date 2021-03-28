import xsimlab as xs


@xs.process
class Inout1:
    var = xs.variable(intent="inout")


@xs.process
class In1:
    var = xs.foreign(Inout1, "var")


@xs.process
class Out1:
    var = xs.foreign(Inout1, "var", intent="out")


@xs.process
class Out2:
    pass  # var = xs.foreign(Inout1, "var", intent="out")


@xs.process
class In2:
    var = xs.foreign(Inout1, "var")


@xs.process
class Inout2:
    var = xs.foreign(Inout1, "var", intent="inout")


@xs.process
class Inout3:
    var = xs.foreign(Inout1, "var", intent="inout")


@xs.process
class In3:
    var = xs.foreign(Inout1, "var")


@xs.process
class Other:
    pass
