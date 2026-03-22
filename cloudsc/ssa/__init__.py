"""ssa — Static Single Assignment passes for DaCe SDFGs.

- data_ssa: SSA for data descriptors (Scalars written more than once)
- loop_var_ssa: SSA for loop iteration variables (unique itervar per LoopRegion)
- scalar_privatization: Replace loop-private scalars with fresh transients
- scalar_expansion: Expand scalars to arrays indexed by inner-loop itervar
- unroll: Propagate constant symbols, then unroll matching maps/loops
"""

from ssa.data_ssa import ssa_transform, ssa_transform_wtr
from ssa.loop_var_ssa import isolate_loop_variables
from ssa.scalar_privatization import privatize_scalars
from ssa.scalar_expansion import expand_scalars
from ssa.unroll import propagate_constants, unroll_loops

__all__ = ["ssa_transform", "ssa_transform_wtr", "isolate_loop_variables", "privatize_scalars",
           "expand_scalars", "propagate_constants", "unroll_loops"]
