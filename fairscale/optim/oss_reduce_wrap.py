# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
A module wrapper to go with a Sharded Optimizer in order to handle targeted gradient reduction/gathering automatically.
"""

from typing import Any, Dict, List, Optional, cast

import torch
from torch import Tensor, nn
import torch.distributed as dist
from torch.nn import Parameter

from fairscale.optim.oss import OSS


def _get_global_rank(group: Any, rank: int) -> int:
    if group is dist.group.WORLD:
        return rank
    else:
        global_rank = dist.distributed_c10d._get_global_rank(group, rank)  # type: ignore
    return global_rank


class ModelDispatch(nn.Module):
    """
    Wrap a model, make it possible to load parameters on the fly for the FW pass and gather gradients.
    Depending on whether this rank is or is not the `owner_rank`, this ModelShard either only handles
    a shard of the compute and is stateless or also owns the up to date state.
    """

    def __init__(
        self,
        base_model: nn.Module,
        sharded_optimizer: OSS,
        process_group: Any,
        broadcast_buffers: bool = True,
        reference_rank: int = 0,
        buffer_size: int = 2 ** 19,
    ):
        super().__init__()
        self.process_group = process_group if process_group is not None else dist.group.WORLD
        self.base_model = base_model
        self.sharded_optimizer = sharded_optimizer
        self.rank = dist.get_rank(self.process_group)
        self.global_rank = _get_global_rank(self.process_group, dist.get_rank(self.process_group))
        self.reference_global_rank = _get_global_rank(self.process_group, reference_rank)
        self.world_size = dist.get_world_size(self.process_group)
        self.broadcast_buffers = broadcast_buffers

        # Allocate reduce buffers
        # - Never use a bigger buffer than the number of model params
        buffer_size = min(buffer_size, sum(p.numel() for p in self.base_model.parameters()))
        self._reduce_buffers: Dict[torch.device, List[torch.Tensor]] = {}

        # - One buffer per rank per device
        for device, per_device in self.sharded_optimizer.per_device_params.items():
            buffer_dtype = per_device[0][0].dtype
            self._reduce_buffers[device] = [
                torch.zeros(buffer_size, dtype=buffer_dtype, device=device) for _ in range(len(per_device))
            ]

    def forward(self, *inputs):  # type: ignore
        if self.broadcast_buffers and len(list(self.base_model.buffers())) > 0:
            self.sync_buffers(non_blocking=False)

        return (self.base_model(*inputs),) if isinstance(inputs, tuple) else self.base_model(inputs)

    def dispatch_grads(self) -> None:
        """
        Reduce -NOTE: could become gather- all the gradients to the appropriate ranks
        """
        with torch.no_grad():
            for device, per_device in self.sharded_optimizer.per_device_params.items():
                self._reduce_grads_task(
                    self._reduce_buffers[device],
                    per_device,
                    group=self.process_group,
                    self_rank=self.rank,
                    world_size=self.world_size,
                )

    @staticmethod
    def _reduce_grads_task(
        buffers: List[torch.Tensor], per_rank_params: List[List[Parameter]], group: Any, self_rank: int, world_size: int
    ) -> None:
        """Helper to reduce a list of params. The params are sorted by size, smallest first, which allows for
        an opportunistic bucketing.

        NOTE: All param gradients are assumed to exist"""

        buffer_size = buffers[0].numel()
        bucket_requests = []
        requests = []

        for (rank, params), buffer in zip(enumerate(per_rank_params), buffers):
            # All the params are sorted per rank and per increasing size
            if len(params) == 0:
                continue

            for p in params:
                if p.grad is None:
                    p.grad = torch.zeros_like(p)

            global_rank = _get_global_rank(group, rank)

            # Copy small gradients into per-GPU buffers and then async reduce
            i_bucketed = 0  # the number of tensors packed in the buffer
            offset = 0

            # Since all the parameters are already sorted per increasing size, we only need to consider the first ones.
            while i_bucketed < len(params) and offset + params[i_bucketed].numel() < buffer_size:
                end = offset + params[i_bucketed].numel()
                buffer[offset:end].copy_(params[i_bucketed].grad.data.view(-1))  # type: ignore
                offset = end
                i_bucketed += 1

            if i_bucketed > 0:
                buffer.div_(world_size)  # type: ignore
                bucket_requests.append(
                    (
                        dist.reduce(tensor=buffer, dst=global_rank, group=group, async_op=True),  # type: ignore
                        rank,
                    )
                )

            # Directly reduce the other grads
            for p in params[i_bucketed:]:
                p.grad = cast(Tensor, p.grad)
                if p.grad.requires_grad:
                    raise RuntimeError("DistributedDataParallel only works with gradients that don't require grad")

                p.grad.div_(world_size)  # type: ignore
                requests.append(dist.reduce(tensor=p.grad, dst=global_rank, group=group, async_op=True))  # type: ignore

        # Unroll the initial packed small gradients, as soon as possible
        for future, rank in bucket_requests:
            future.wait()

            if rank == self_rank:
                i_bucketed = 0  # the number of tensors packed in the buffer
                offset = 0
                params = per_rank_params[rank]
                buffer = buffers[rank]

                while i_bucketed < len(params) and offset + params[i_bucketed].numel() < buffer_size:
                    end = offset + params[i_bucketed].numel()
                    params[i_bucketed].grad.data.copy_(buffer[offset:end].view_as(params[i_bucketed]))  # type: ignore
                    offset = end
                    i_bucketed += 1

        # Make sure that we're done with this device before moving on and cleaning the unused params
        _ = list(map(lambda x: x.wait(), requests))

    def sync_buffers(self, non_blocking: bool = True) -> Optional[List[Any]]:
        """
        Sync all the param buffers in between ranks.
        TODO: Could be worth bucketing ?
        """
        requests = list(
            map(
                lambda x: dist.broadcast(x, self.reference_global_rank, self.process_group, async_op=True),
                self.base_model.buffers(),
            ),
        )
        return requests if non_blocking else self.wait(requests)

    def sync_parameters(self, non_blocking: bool = True) -> Optional[List[Any]]:
        """
        Sync all the parameters in between ranks.
        TODO: Could be worth bucketing ?
        """
        requests = list(
            map(
                lambda x: dist.broadcast(x, self.reference_global_rank, self.process_group, async_op=True),
                self.base_model.parameters(),
            ),
        )
        return requests if non_blocking else self.wait(requests)

    @staticmethod
    def wait(requests: Optional[List[Any]]) -> None:
        """
        Make an async function synchronous.
        Use this to wrap the function call directly
        """
        if requests:
            _ = list(map(lambda x: x.wait(), requests))
        return


class DispatchLayer(torch.autograd.Function):
    """
     The dispatch layer is a synchronization point between model shards.

     - In the forward pass it does nothing
     - In the backward pass, it gathers gradients to the owner.

     NOTE: see https://pytorch.org/docs/stable/autograd.html#torch.autograd.Function
     """

    @staticmethod
    def forward(ctx: Any, model: ModelDispatch, *inputs: Any) -> Any:  # type: ignore
        # Store a handle to the model for the BW dispatch
        ctx.model = model

        # Return inputs as-is
        outputs = inputs
        return outputs

    @staticmethod
    def backward(ctx, *grad_outputs):  # type: ignore
        ctx.model.dispatch_grads()

        # The returned variables need to mirror the forward inputs
        if isinstance(grad_outputs, tuple):
            return None, None, grad_outputs[0]

        return None, None, grad_outputs


class GradReducerModelWrap(nn.Module):
    """
    Wrap the model, and make sure that the gradients will be reduced to the right rank.

    - the partition is given by the sharded optimizer
    - wrap the base model with a model which knows where to reduce each gradient
    - add an autograd function which calls this on the way back
    """

    def __init__(
        self,
        base_model: nn.Sequential,  # hard pre-requisite for now, easier model slicing
        sharded_optimizer: OSS,
        world_size: int,
        process_group: Any = None,
        broadcast_buffers: bool = True,
    ):
        super().__init__()

        self.world_size = world_size
        self.process_group = process_group if process_group is not None else torch.distributed.group.WORLD
        self.rank = dist.get_rank(self.process_group)
        self.global_rank = _get_global_rank(self.process_group, self.rank)
        self.backend = dist.get_backend(group=self.process_group)  # type: ignore
        self.model_dispatch = ModelDispatch(
            base_model=base_model,
            sharded_optimizer=sharded_optimizer,
            process_group=process_group,
            broadcast_buffers=broadcast_buffers,
            reference_rank=0,
        )

    def forward(self, *inputs: Any, **kwargs: Any) -> Any:
        # All inputs need to required_grad to properly track the first sync layer
        if isinstance(inputs, tuple):
            for i in inputs:
                i.requires_grad = True
        elif isinstance(inputs, torch.Tensor):
            inputs.requires_grad = True

        # Register the model dispatch in the autograd graph
        DispatchLayer.apply(self.model_dispatch, *inputs)

        # Normal model FW
        outputs = self.model_dispatch(*inputs)
        return outputs[0] if len(outputs) == 1 else outputs