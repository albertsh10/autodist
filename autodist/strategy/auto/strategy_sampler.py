# Copyright 2020 Petuum. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Strategy sampler that generates random strategies given model and resource spec."""

from collections import OrderedDict

from enum import Enum
from tensorflow.python.framework import ops

from autodist.kernel.common.utils import get_op_name, get_consumers
from autodist.kernel.partitioner import PartitionerConfig
from autodist.proto import strategy_pb2, synchronizers_pb2
from autodist.strategy.base import Strategy, StrategyBuilder, byte_size_load_fn
from autodist.strategy.auto.ps_load_balancer import greedy_load_balancer, christy_load_balancer
from autodist.strategy.auto.ar_group_assigner import chunk_group_assigner, christy_group_assigner, \
    ordered_balanced_group_assigner
from autodist.strategy.auto import sample_util
from autodist.const import MAX_INT32


class VarType(Enum):
    SPARSE = 0
    DENSE = 1


class VariableHelper:
    """Helper class to include meta information about a variable."""
    def __init__(self, var, graph_item):
        self.var = var
        self.graph_item = graph_item
        self._var_op_name = get_op_name(var.name)
        self._grad = graph_item.var_op_name_to_grad_info[self._var_op_name][0]

    @property
    def var_type(self):
        """
        Return the type of the variable (VarType.SPARSE or VarType.DENSE).

        Returns:
            VarType
        """
        return VarType.DENSE if isinstance(self._grad, ops.Tensor) else VarType.SPARSE

    @property
    def is_sparse(self):
        """
        Return whether the variable is sparse.

        Returns:
            Bool
        """
        return True if self.var_type == VarType.SPARSE else False

    @property
    def is_embedding(self):
        """
        Return whether the variable corresponds to an embedding.

        Returns:
            Bool
        """
        # TODO (Hao): better way to determine is_embedding?
        for op in get_consumers(self.var.op):
            if op.type == "ResourceGather":
                return True
        return False

    @property
    def shape(self):
        """
        Return the shape of the variable, or None if it does not emit a tensor (e.g. scalar).

        Returns:
            List(int)
        """
        if self.var.initial_value.shape.ndims:
            return self.var.initial_value.shape.as_list()
        else:
            return None

    @property
    def partitionable_axes(self):
        """
        Return the list of available axes that are legitimate to partition along.

        Returns:
            List(int)
        """
        valid_axes = []

        # scalar
        if not self.shape:
            return valid_axes

        # Sparse variable can only be partition along the 0th axis in current implementation.
        if self.is_sparse or self.is_embedding:
            valid_axes = [0]
            return valid_axes
        for idx, dim in enumerate(self.shape):
            if dim > 1:
                valid_axes.append(idx)
        return valid_axes

    @property
    def byte_size(self):
        """
        Return the byte size of the variable.

        Returns:
            float
        """
        return float(byte_size_load_fn(self.var))

    @property
    def dtype(self):
        """
        Return the dtype of the variable.

        Returns:
            dtype
        """
        return self.var.dtype


class PartHelper:
    """Helper class to include meta information about a variable partition."""
    def __init__(self, part_idx, var, pc):
        self.var = var
        self.part_idx = part_idx
        self.pc = pc

    @property
    def shape(self):
        """
        Return the shape of this partition.

        Returns:
            List(int)

        """
        shape = self.var.initial_value.shape.as_list()
        dim_size = shape[self.pc.axis] // self.pc.num_shards
        extras = shape[self.pc.axis] % self.pc.num_shards
        if self.part_idx < extras:
            dim_size += 1
        shape[self.pc.axis] = dim_size
        return shape

    @property
    def var_shape(self):
        """
        Return the shape of the original value this part belonged to.

        Returns:
            List(int)
        """
        return self.var.initial_value.shape.as_list()

    @property
    def byte_size(self):
        """
        Return the byte size of this partition.

        Returns:
            float
        """
        return float(byte_size_load_fn(self.var)) \
               * float(self.shape[self.pc.axis]) / float(self.var_shape[self.pc.axis])


class RandomStrategySampler():
    """
    Random Strategy Sampler.

    This StrategyBuilder samples a strategy given graph_item and resource_spec. The sampling process is
    constrained by `space`, and guided by `heuristics`, both as required arguments of its constructor.
    """
    def __init__(self, space, heuristics):
        """

        Args:
            space (dict): the strategy space that the random strategy should be drawn from. An example of the space
                          can be found at TODO(Hao).
            heuristics (dict): heuristics used to guide the random sampling process.
        """
        if not space:
            raise ValueError('Space to perform strategy sampling is not provided.')
        if not heuristics:
            raise ValueError('Heuristic to guide strategy sampling is not provided.')
        self.space = space
        self.heuristics = heuristics
        self.helpers = {}

    def build(self, graph_item, resource_spec):
        """Generate a randomized strategy given model and resource spec."""
        expr = Strategy()

        # number of graph replica is equal to number of GPU devices
        expr.graph_config.replicas.extend([k for k, v in resource_spec.gpu_devices])
        variables = graph_item.trainable_var_op_to_var.values()

        # Perform MCMC to generate each node configs
        node_config = []
        for var in variables:
            var_helper = VariableHelper(var, graph_item)
            self.helpers[var_helper.var.name] = var_helper

            node = strategy_pb2.Strategy.Node()
            node.var_name = var_helper.var.name

            # Step 1: determine whether or not to partition
            # TODO(Hao): some factor is not considered, e.g. number of reduction_device_names
            maybe_partition = sample_if_partition(var_helper, resource_spec, self.space, self.heuristics)

            # Step 2.1: if not partition, sample a synchronizer type for it
            if not maybe_partition:  # no partition
                sample_var_synchronizer(node, var_helper, resource_spec, self.space)
            else:  # Step 2.2: else partition
                # Step 2.2.1: sample a partitioner config
                pc = sample_partition_config(var_helper, resource_spec, self.space, self.heuristics)
                node.partitioner = pc.partition_str

                # step 2.2.2: sample a synchronizer type for each partition
                parts = []
                for i in range(pc.num_shards):
                    part = strategy_pb2.Strategy.Node()
                    part.var_name = '{}/part_{}:0'.format(get_op_name(var.name), i)
                    self.helpers[part.var_name] = PartHelper(i, var, pc)
                    parts.append(part)
                sample_parts_synchronizers(parts, var_helper, resource_spec, self.space, self.heuristics)
                node.part_config.extend(parts)
            node_config.append(node)

        # Step 3: Post-assign group or placement.
        sample_group_and_reduction_destinations(node_config, resource_spec, self.helpers, self.heuristics)

        expr.node_config.extend(node_config)
        self._reset()
        return expr

    def _reset(self):
        """Reset the helpers every time a strategy is sampled."""
        self.helpers = {}


def sample_if_partition(var_helper, resource_spec, space, heuristics):
    """
    Sample a bool value determining whether to partition a variable or not.

    Args:
        var_helper: the variable helper corresponded to the variable of interest.
        resource_spec: the target cluster spec.
        space: the space argument controlling where to sample from.
        heuristics: the heuristics argument  guiding the sampling process.

    Returns:
        Bool
    """
    reduction_device_names = [k for k, _ in resource_spec.cpu_devices]
    if len(space['maybe_partition']) == 1:
        return space['maybe_partition']
    if heuristics['enable_single_node_no_partition'] and len(reduction_device_names) <= 1:
        return False

    # intersection of variable's partitonable axis and global constraints
    if var_helper.partitionable_axis:
        if space['partitionable_axis']:
            a = set(var_helper.partitionable_axis) & set(space['partitionable_axis'])
            if len(a) < 1:
                return False
    else:
        return False

    # lower bound for abandoning partitioning
    lb = heuristics['maybe_partition_bounds'][0]
    ub = heuristics['maybe_partition_bounds'][1]
    if var_helper.byte_size <= lb:
        return False
    if var_helper.byte_size >= ub:
        return True
    assert (len(space['maybe_partition']) == 2)

    if heuristics['maybe_partition_by_size']:
        #  By variable size -- a large variable has a higher chance to be partitioned
        # TODO (Hao): MAX_INT32 is too large, reconsider later...
        chance = float(var_helper.byte_size - lb) / float(ub - lb)
        return sample_util.binary_sample(boundary=chance)
    else:
        return sample_util.uniform_sample_by_choices(space['maybe_partition'])


def sample_var_synchronizer(node, var_helper, resource_spec, space):
    """
    Sample a synchronizer (and all associated aspects) for an unpartitioned variable,
    leaving merge_group or reduction_destination as empty.

    Args:
        node (strategy_pb2.Strategy.Node): the corresponded node_config to be rewritten.
        var_helper (VariableHelper): the variable helper corresponded to the variable.
        resource_spec (ResourceSpec): the target cluster spec
        space (dict): space.
    """
    # We ALWAYS use PS for sparse variables
    synchronizer_type = 'PS' if var_helper.var_type == VarType.SPARSE \
        else sample_util.uniform_sample_by_choices(space['synchronizer_types'])
    if synchronizer_type == 'PS':
        node.PSSynchronizer.sync = True  # we don't consider async at this moment
        node.PSSynchronizer.staleness = 0
        node.PSSynchronizer.local_replication = sample_if_local_replication(space['local_replication'],
                                                                            resource_spec)
    else:
        # no other option for spec
        node.AllReduceSynchronizer.spec = synchronizers_pb2.AllReduceSynchronizer.Spec.Value('AUTO')
        node.AllReduceSynchronizer.compressor = \
            synchronizers_pb2.AllReduceSynchronizer.Compressor.Value(
                sample_ar_compressor(space['compressor']))


def sample_parts_synchronizers(parts, var_helper, resource_spec, space, heuristics):
    """
    Sample synchronizers for all the partitions of a variable.

    Args:
        parts:
        var_helper:
        resource_spec:
        space:
        heuristics:

    Returns:
    """
    if var_helper.var_type == VarType.SPARSE:
        synchronizer_types = ['PS'] * len(parts)
    else:
        if heuristics['same_synchronizer_for_parts']:
            type = sample_util.uniform_sample_by_choices(space['synchronizer_types'])
            synchronizer_types = [type] * len(parts)
        else:
            synchronizer_types = [sample_util.uniform_sample_by_choices(space['synchronizer_types'])
                                  for part in parts]
    for i, part in enumerate(parts):
        if synchronizer_types[i] == 'PS':
            part.PSSynchronizer.sync = True  # we don't consider async at this moment
            part.PSSynchronizer.staleness = 0
            part.PSSynchronizer.local_replication = sample_if_local_replication(space['local_replication'],
                                                                                resource_spec)
        else:
            # no other option for spec
            part.AllReduceSynchronizer.spec = synchronizers_pb2.AllReduceSynchronizer.Spec.Value('AUTO')
            part.AllReduceSynchronizer.compressor = \
                synchronizers_pb2.AllReduceSynchronizer.Compressor.Value(
                    sample_ar_compressor(space['compressor']))


def sample_partition_config(var_helper, resource_spec, space, heuristics):
    """
    Sample the PartitionerConfig of a variable (that is to be partitioned).

    Args:
        var_helper:
        resource_spec:
        space:
        heuristics:

    Returns:
    """
    # Arion only support partitioning along one axis -- we first sample a partition axis,
    # then sample the number of partitions along that axis, and obtain the partition config.
    assert len(var_helper.partitionable_axis) > 0, 'No partition axis available'
    # sample partition axis
    # TODO(Hao): some heursitics here available?
    valid_axis = var_helper.partitionable_axis
    if space['partitionable_axis']:
        valid_axis = list(set(valid_axis) & set(space['partitionable_axis']))
    partition_axis = sample_util.uniform_sample_by_choices(valid_axis)

    # sample how many partition to go
    num_nodes = resource_spec.num_cpus
    dim_size = var_helper.shape[partition_axis]
    if heuristics['num_partition_bounds'][1] == 'num_nodes':
        max_shards = min(dim_size, num_nodes)
    elif isinstance(heuristics['num_partition_bounds'][1], int):
        max_shards = min(dim_size, heuristics['num_partition_bounds'][1])
    else:
        raise ValueError('unseen num_partition_bounds config')

    min_shards = 2
    if isinstance(heuristics['num_partition_bounds'][0], int):
        min_shards = max(min_shards, heuristics['num_partition_bounds'][0])
    elif heuristics['num_partition_bounds'][0] == 'num_nodes':
        min_shards = max(min_shards, heuristics['num_partition_bounds'][0])
    else:
        raise ValueError('unseen num_partition_bounds config')

    # sample from [min_shards, max_shards]
    num_shards = sample_util.uniform_sample_by_choices(range(min_shards, max_shards + 1))

    # construct a PartitionerConfig (pc)
    partition_list = [1] * len(var_helper.shape)
    partition_list[partition_axis] = num_shards
    pc = PartitionerConfig(partition_list=partition_list)
    return pc


def sample_if_local_replication(local_replication_space, resource_spec):
    """
    Sample whether to perform local replication.

    Local replication is a PS-specific semantic; it represents whether to transfer parameters or updates
    via a transfer device.

    Args:
        local_replication_space:
        resource_spec:

    Returns:

    """
    if resource_spec.num_gpus <= resource_spec.num_cpus:
        # meaning every machine has at most 1 GPU
        return False
    return sample_util.uniform_sample_by_choices(local_replication_space)


def sample_ar_compressor(compressor_space):
    """
    Sample the type of the compressor being applied with collective ops.

    Available options include `NoneCompressor`, `HorovodCompressor`, `HorovodCompressorEF`,
    `PowerSGDCompressor`, but `HorovodCompressorEF`, `PowerSGDCompressor` will change gradient value.
    Args:
        compressor_space:

    Returns:
    """
    # TODO(Hao): try to use all four options
    return sample_util.uniform_sample_by_choices(compressor_space)


def sample_group_and_reduction_destinations(node_config, resource_spec, helpers, heuristics):
    """
    Sample the merge group or parameter placement (a.k.a. reduction_destination) after all other semantics
    have been determined.

    Args:
        node_config:
        resource_spec:
        helpers:
        heuristics:

    Returns:

    """
    ps_shards = OrderedDict()
    ar_shards = OrderedDict()
    idx = 0
    for node in node_config:
        if node.partitioner:
            for part in node.part_config:
                synchronizer = getattr(part, part.WhichOneof('synchronizer'))
                if hasattr(synchronizer, 'compressor'):
                    ar_shards[part.var_name] = (idx,)
                else:
                    ps_shards[part.var_name] = (idx,)
                idx += 1
        else:
            synchronizer = getattr(node, node.WhichOneof('synchronizer'))
            if hasattr(synchronizer, 'compressor'):
                ar_shards[node.var_name] = (idx,)
            else:
                ps_shards[node.var_name] = (idx,)
            idx += 1

    if len(ps_shards) > 0:
        sample_ps_reduction_destinations(node_config, ps_shards, resource_spec, helpers, heuristics)
    if len(ar_shards) > 0:
        sample_ar_groups(node_config, ar_shards, helpers, heuristics)


def sample_ps_reduction_destinations(node_config, ps_shards, resource_spec, helpers, heuristics):
    """
    Sample the placement of shared parameter variables (a.k.a. reduction destinations).

    Args:
        node_config:
        ps_shards:
        resource_spec:
        helpers:
        heuristics:

    Returns:

    """
    load_balancer = heuristics['ps_load_balancer']
    reduction_device_names = [k for k, _ in resource_spec.cpu_devices]
    if not load_balancer:
        destinations = {}
        for shard_name in ps_shards:
            destinations[shard_name] = sample_util.uniform_sample_by_choices(reduction_device_names)
    elif load_balancer == 'greedy':
        destinations = greedy_load_balancer(ps_shards, resource_spec, helpers)
    elif load_balancer == 'christy':
        # copy Christy's partitionedPS
        destinations = christy_load_balancer(ps_shards, resource_spec, helpers)
    elif load_balancer == 'sorted_christy':
        destinations = christy_load_balancer(ps_shards, resource_spec, helpers, sort_by_size=True)
    elif load_balancer == 'sorted_greedy':
        destinations = greedy_load_balancer(ps_shards, resource_spec, helpers, sort_by_size=True)
    else:
        raise ValueError('Cannot recognize load balancer')

    for shard_name, (idx, ) in ps_shards.items():
        ps_shards[shard_name] = (idx, destinations[shard_name])

    assign_ps_reduction_destinations(node_config, ps_shards)


def assign_ps_reduction_destinations(node_config, ps_shards):
    """
    Assign the sampled reduction destinations to node_config.

    Args:
        node_config:
        ps_shards:

    Returns:

    """
    for node in node_config:
        if node.partitioner:
            for part in node.part_config:
                synchronizer = getattr(part, part.WhichOneof('synchronizer'))
                if hasattr(synchronizer, 'reduction_destination'):
                    synchronizer.reduction_destination = ps_shards[part.var_name][1]
        else:
            synchronizer = getattr(node, node.WhichOneof('synchronizer'))
            if hasattr(synchronizer, 'reduction_destination'):
                synchronizer.reduction_destination = ps_shards[node.var_name][1]


def sample_ar_groups(node_config, ar_shards, helpers, heuristics):
    """
    Sample the group of collective operations.

    Args:
        node_config:
        ar_shards:
        helpers:
        heuristics:

    Returns:

    """
    merge_scheme = heuristics['merge_scheme']
    if merge_scheme == 'by_chunk':
        if 'chunk_size' in heuristics and heuristics['chunk_size'] > 0:
            chunk_size_or_num_group = heuristics['chunk_size']
        else:
            chunk_size_or_num_group = sample_chunk_size(len(ar_shards))
    else:
        chunk_size_or_num_group = sample_num_ar_groups(ar_shards,
                                                       heuristics['num_group_bounds'][0],
                                                       heuristics['num_group_bounds'][1])
    assert chunk_size_or_num_group > 0, "chunk_size or num_groups need to > 1..."

    if merge_scheme in ['random', None]:
        tmp_assignments = sample_util.sample_merge_group(chunk_size_or_num_group, len(ar_shards))
        group_assignments = OrderedDict()
        for i, shard_name in enumerate(ar_shards):
            group_assignments[shard_name] = tmp_assignments[i]
    elif merge_scheme == 'by_chunk':
        # sample chunk_size
        group_assignments = chunk_group_assigner(ar_shards, chunk_size_or_num_group)
    elif merge_scheme == 'christy':
        group_assignments = christy_group_assigner(ar_shards,
                                                   helpers,
                                                   chunk_size_or_num_group)
    elif merge_scheme == 'ordered_balanced':
        group_assignments = ordered_balanced_group_assigner(ar_shards,
                                                            helpers,
                                                            chunk_size_or_num_group)
    else:
        raise ValueError('unseen merge scheme..')

    for shard_name, (idx,) in ar_shards.items():
        ar_shards[shard_name] = (idx, group_assignments[shard_name])
    assign_ar_group(node_config, ar_shards)


def sample_num_ar_groups(ar_shards, lb, ub):
    """
    Sample the number of collective groups.

    Args:
        ar_shards:
        lb:
        ub:

    Returns:

    """
    min_num_group = max(1, lb)
    max_num_group = min(len(ar_shards), ub)
    num_group = sample_util.uniform_sample_by_choices(list(range(min_num_group, max_num_group + 1)))
    return num_group


def sample_chunk_size(num_ar_shards):
    """
    Sample the chunk_size if following a chunk-based merge scheme.

    Args:
        num_ar_shards:

    Returns:

    """
    chunk_size = sample_util.uniform_sample_by_choices(list(range(1, num_ar_shards + 1)))
    return chunk_size


def assign_ar_group(node_config, ar_shards):
    """
    Assign the sampled group values to node configs.

    Args:
        node_config:
        ar_shards:

    Returns:

    """
    for node in node_config:
        if node.partitioner:
            for part in node.part_config:
                synchronizer = getattr(part, part.WhichOneof('synchronizer'))
                if hasattr(synchronizer, 'compressor'):
                    synchronizer.group = ar_shards[part.var_name][1]
        else:
            synchronizer = getattr(node, node.WhichOneof('synchronizer'))
            if hasattr(synchronizer, 'compressor'):
                synchronizer.group = ar_shards[node.var_name][1]


default_space = {
    'synchronizer_types': ['PS', 'AR'],
    'maybe_partition': [True, False],
    'compressor': ['HorovodCompressor', 'NoneCompressor', 'HorovodCompressorEF'],
    'local_replication': [False],
    'partitionable_axis': []
}


default_heuristics = {
    'ps_load_balancer': None, # None, 'christy', 'greedy', 'LP'
    'merge_scheme': None,  # random, by_chunk, christy, ordered_balanced
    'chunk_size': -1,
    'num_group_bounds': [-1, MAX_INT32],
    'maybe_partition_bounds': [0, MAX_INT32],
    'maybe_partition_by_size': None,
    'num_partition_bounds': [2, MAX_INT32],
    'enable_single_node_no_partition': False,
    'same_synchronizer_for_parts': False,
}