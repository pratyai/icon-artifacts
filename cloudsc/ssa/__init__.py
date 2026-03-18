"""ssa — Static Single Assignment passes for DaCe SDFGs.

- data_ssa: SSA for data descriptors (Scalars written more than once)
- loop_var_ssa: SSA for loop iteration variables (unique itervar per LoopRegion)
"""

from ssa.data_ssa import ssa_transform
from ssa.loop_var_ssa import isolate_loop_variables

__all__ = ["ssa_transform", "isolate_loop_variables"]
