"""Read-only classifier of array roles for GPU offloading.

Populates ``sdfg._array_roles: Dict[str, ArrayRole]``.

The classifier walks the SDFG top-to-bottom, attributes every AccessNode to
a side (Host / Device) based on the nearest enclosing Map schedule, records
reads/writes with topological rank, and derives whether each array is pure
host, pure device, or dual-access.

It also identifies "mixed-access maps" — maps that touch both host-only
and device-only arrays — and stores them for the caller to handle via
explicit policy. The classifier itself never mutates the SDFG.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import dace
from dace import dtypes
from dace.sdfg.analysis import cfg as cfg_analysis
from dace.sdfg.nodes import AccessNode, MapEntry, MapExit, NestedSDFG, Tasklet
from dace.sdfg.state import SDFGState


class Side(Enum):
    Host = "host"
    Device = "device"


@dataclass
class AccessRef:
    sdfg_label: str
    state_label: str
    node_id: int            # state.node_id(access_node)
    side: Side
    is_write: bool
    subset: str             # stringified — we don't need to compute on it here
    topo_rank: int          # monotonic; larger = later in execution

    def __repr__(self):
        kind = "W" if self.is_write else "R"
        return (f"{self.sdfg_label}/{self.state_label}[{self.node_id}] "
                f"{self.side.value}:{kind} {self.subset} @rank={self.topo_rank}")


@dataclass
class ArrayRole:
    name: str
    accesses: List[AccessRef] = field(default_factory=list)

    @property
    def host_writes(self) -> List[AccessRef]:
        return [a for a in self.accesses if a.side is Side.Host and a.is_write]

    @property
    def host_reads(self) -> List[AccessRef]:
        return [a for a in self.accesses if a.side is Side.Host and not a.is_write]

    @property
    def device_writes(self) -> List[AccessRef]:
        return [a for a in self.accesses if a.side is Side.Device and a.is_write]

    @property
    def device_reads(self) -> List[AccessRef]:
        return [a for a in self.accesses if a.side is Side.Device and not a.is_write]

    @property
    def touches_host(self) -> bool:
        return any(a.side is Side.Host for a in self.accesses)

    @property
    def touches_device(self) -> bool:
        return any(a.side is Side.Device for a in self.accesses)

    @property
    def is_dual(self) -> bool:
        return self.touches_host and self.touches_device


@dataclass
class MixedAccessMap:
    sdfg_label: str
    state_label: str
    map_label: str
    host_arrays: Set[str]
    device_arrays: Set[str]


@dataclass
class ClassifierResult:
    array_roles: Dict[str, ArrayRole]
    mixed_access_maps: List[MixedAccessMap]


def _enclosing_side(state: SDFGState, node) -> Side:
    """Return Device iff some enclosing Map in this state is GPU_Device-scheduled."""
    scope_dict = state.scope_dict()
    cur = scope_dict.get(node)
    while cur is not None:
        if isinstance(cur, MapEntry) and cur.map.schedule == dtypes.ScheduleType.GPU_Device:
            return Side.Device
        cur = scope_dict.get(cur)
    return Side.Host


def _edge_side(state: SDFGState, edge) -> Side:
    """Classify an AccessNode-adjacent edge by the other endpoint.

    - Edge touches a MapEntry/MapExit with GPU schedule → Device.
    - Edge touches a NestedSDFG whose own enclosing scope is GPU → Device.
      (v1: we treat NSDFGs conservatively as matching the scope they sit in.)
    - Otherwise → Host.
    """
    other = edge.dst if isinstance(edge.src, AccessNode) else edge.src
    if isinstance(other, (MapEntry, MapExit)):
        if other.map.schedule == dtypes.ScheduleType.GPU_Device:
            return Side.Device
        # Map might be nested inside a GPU map — defer to enclosing-side.
        return _enclosing_side(state, other)
    if isinstance(other, NestedSDFG):
        return _enclosing_side(state, other)
    if isinstance(other, AccessNode):
        # AccessNode-to-AccessNode: a copy. The "side" of this edge is the
        # enclosing scope (same for both ends in practice).
        return _enclosing_side(state, other)
    # Tasklet or other code node — use its enclosing scope.
    return _enclosing_side(state, other)


def classify(sdfg: dace.SDFG) -> ClassifierResult:
    """Build and return the ClassifierResult; also attach it to sdfg._array_roles."""
    roles: Dict[str, ArrayRole] = {}
    mixed: List[MixedAccessMap] = []
    topo_counter = [0]

    def _walk(cur_sdfg: dace.SDFG):
        ordered = list(cfg_analysis.blockorder_topological_sort(
            cur_sdfg, recursive=True, ignore_nonstate_blocks=False))
        for block in ordered:
            if not isinstance(block, SDFGState):
                continue
            if block.sdfg is not cur_sdfg:
                # Let the deeper walk handle this one.
                continue
            for node in block.nodes():
                if isinstance(node, AccessNode):
                    name = node.data
                    if name not in cur_sdfg.arrays:
                        continue
                    for e in block.in_edges(node):
                        subset = e.data.dst_subset or e.data.subset
                        roles.setdefault(name, ArrayRole(name)).accesses.append(
                            AccessRef(
                                sdfg_label=cur_sdfg.label,
                                state_label=block.label,
                                node_id=block.node_id(node),
                                side=_edge_side(block, e),
                                is_write=True,
                                subset=str(subset) if subset is not None else "",
                                topo_rank=topo_counter[0],
                            )
                        )
                    for e in block.out_edges(node):
                        subset = e.data.src_subset or e.data.subset
                        roles.setdefault(name, ArrayRole(name)).accesses.append(
                            AccessRef(
                                sdfg_label=cur_sdfg.label,
                                state_label=block.label,
                                node_id=block.node_id(node),
                                side=_edge_side(block, e),
                                is_write=False,
                                subset=str(subset) if subset is not None else "",
                                topo_rank=topo_counter[0],
                            )
                        )
                    topo_counter[0] += 1

                elif isinstance(node, MapEntry) and node.map.schedule == dtypes.ScheduleType.GPU_Device:
                    # Collect mixed-access info: look at the arrays that feed into
                    # this map vs. any host-only arrays reachable from its body.
                    # (v1 heuristic: just record per-map touched arrays by side.)
                    host_arrs: Set[str] = set()
                    device_arrs: Set[str] = set()
                    subgraph = block.scope_subgraph(node, include_entry=False, include_exit=False)
                    for n in subgraph.nodes():
                        if isinstance(n, AccessNode):
                            n_side = _enclosing_side(block, n)
                            (device_arrs if n_side is Side.Device else host_arrs).add(n.data)
                    if host_arrs and device_arrs:
                        mixed.append(MixedAccessMap(
                            sdfg_label=cur_sdfg.label,
                            state_label=block.label,
                            map_label=node.map.label,
                            host_arrays=host_arrs,
                            device_arrays=device_arrs,
                        ))

                elif isinstance(node, NestedSDFG):
                    _walk(node.sdfg)

    _walk(sdfg)

    result = ClassifierResult(array_roles=roles, mixed_access_maps=mixed)
    sdfg._array_roles = result
    return result


def summarize(result: ClassifierResult, names: Optional[List[str]] = None) -> str:
    """Compact printable summary."""
    lines = []
    arrs = result.array_roles
    if names is None:
        names = sorted(arrs.keys())
    for nm in names:
        if nm not in arrs:
            lines.append(f"{nm}: (absent)")
            continue
        r = arrs[nm]
        lines.append(
            f"{nm}: "
            f"hW={len(r.host_writes)} hR={len(r.host_reads)} "
            f"dW={len(r.device_writes)} dR={len(r.device_reads)} "
            f"dual={r.is_dual}"
        )
    if result.mixed_access_maps:
        lines.append("")
        lines.append(f"Mixed-access maps: {len(result.mixed_access_maps)}")
        for m in result.mixed_access_maps[:8]:
            lines.append(
                f"  {m.sdfg_label}/{m.state_label}:{m.map_label}"
                f"  host={sorted(m.host_arrays)[:4]}  device={sorted(m.device_arrays)[:4]}"
            )
    return "\n".join(lines)
