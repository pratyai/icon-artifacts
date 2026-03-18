"""ssa — Static Single Assignment passes for DaCe SDFGs.

- data_ssa: SSA for data descriptors (Scalars written more than once)
- loop_var_ssa: SSA for loop iteration variables (unique itervar per LoopRegion)
- scalar_privatization: Replace loop-private scalars with fresh transients
- scalar_expansion: Expand scalars to arrays indexed by inner-loop itervar
"""

from ssa.data_ssa import ssa_transform
from ssa.loop_var_ssa import isolate_loop_variables
from ssa.scalar_privatization import privatize_scalars
from ssa.scalar_expansion import expand_scalars

__all__ = ["ssa_transform", "isolate_loop_variables", "privatize_scalars",
           "expand_scalars"]
