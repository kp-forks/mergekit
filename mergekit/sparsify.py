# Copyright (C) 2025 Arcee AI
# SPDX-License-Identifier: BUSL-1.1

from enum import Enum

import torch


class SparsificationMethod(str, Enum):
    magnitude = "magnitude"
    random = "random"
    magnitude_outliers = "magnitude_outliers"
    rank_magnitude_sampling = "rank_magnitude_sampling"
    consensus_ta = "consensus_ta"
    consensus_ties = "consensus_ties"


def rescale_sum(tensor: torch.Tensor, mask: torch.Tensor):
    """Rescales the values to match the original tensor sum."""
    org_sum = tensor.abs().sum()
    new_sum = (tensor * mask).abs().sum()

    if org_sum >= 1e-8 and new_sum >= 1e-8:
        tensor *= org_sum / new_sum
    return tensor * mask


def magnitude(tensor: torch.Tensor, density: float, rescale: bool) -> torch.Tensor:
    """Masks out the smallest values, retaining a proportion of `density`."""
    if density >= 1:
        return tensor

    k = int(density * tensor.numel())

    assert k > 0, "not gonna zero out the whole tensor buddy"
    mask = torch.zeros_like(tensor)
    w = tensor.abs().view(-1)
    if w.device.type == "cpu":
        w = w.float()
    topk = torch.argsort(w, descending=True)[:k]
    mask.view(-1)[topk] = 1

    if rescale:
        res = rescale_sum(tensor, mask)
    else:
        res = tensor * mask

    return res


def magnitude_outliers(
    tensor: torch.Tensor, density: float, rescale: bool, gamma: float = 0.01
):
    """Masks out smallest values in addition to large outliers.

    The `gamma` proportion of the largest weights are first removed, then the
    smallest weights are removed to achieve the desired density.

    Args:
        tensor (torch.Tensor): The tensor to sparsify.
        density (float): The proportion of weights to retain.
        gamma (float): Percent of largest weights to remove.
    """
    if density >= 1:
        return tensor

    num_elems = tensor.numel()
    target_n = int(density * num_elems)
    n_top = int(gamma * num_elems)
    n_bot = num_elems - target_n - n_top

    if n_bot < 0:
        # cut down on the number of large weights to remove in
        # order to hit the target density
        n_top += n_bot
        n_bot = 0

    w = tensor.abs().view(-1)
    if w.device.type == "cpu":
        w = w.float()
    indices = torch.sort(w, descending=False).indices
    mask = torch.zeros_like(tensor)

    mask.view(-1)[indices[n_bot:-n_top]] = 1

    if rescale:
        res = rescale_sum(tensor, mask)
    else:
        res = tensor * mask
    return res


def bernoulli(tensor: torch.Tensor, density: float, rescale: bool) -> torch.Tensor:
    if density >= 1:
        return tensor

    if (tensor.device.type != "cpu") or tensor.dtype == torch.bfloat16:
        work_dtype = tensor.dtype
    else:
        # torch.bernoulli not implemented for float16 on CPU, upcast to float32
        work_dtype = torch.float32

    mask = torch.bernoulli(
        torch.full_like(input=tensor, fill_value=density, dtype=work_dtype)
    )
    res = tensor.to(work_dtype) * mask
    if rescale:
        res /= density

    return res.to(tensor.dtype)


def rank_magnitude(
    tensor: torch.Tensor, density: float, rescale: bool = True, epsilon: float = 0.05
) -> torch.Tensor:
    if density >= 1:
        return tensor

    if density <= epsilon or density >= (1 - epsilon):
        raise ValueError(
            f"Error: density +- epsilon must be in the range (0, 1). density + epsilon = {density+epsilon}, density - epsilon = {density-epsilon}"
        )

    if (tensor.device.type != "cpu") or tensor.dtype == torch.bfloat16:
        work_dtype = tensor.dtype
    else:
        work_dtype = torch.float32

    if len(tensor.shape) < 2:
        tensor = tensor.unsqueeze(0)

    # Get Rank matrix for the delta values
    tensor_abs = torch.abs(tensor)

    sorted_indices = torch.argsort(tensor_abs, dim=1, descending=False)

    ranking_tensor = torch.zeros_like(tensor_abs, dtype=work_dtype)
    for i in range(tensor_abs.size(0)):
        ranking_tensor[i][sorted_indices[i]] = torch.arange(
            1, tensor.size(1) + 1, dtype=work_dtype
        ).to(tensor.device)

    # Normalise rank matrix to the probability range to density +- epsilon
    range_vals = (
        ranking_tensor.max(dim=1, keepdim=True).values
        - ranking_tensor.min(dim=1, keepdim=True).values
    )
    norm_metrics = (ranking_tensor - ranking_tensor.min(dim=1, keepdim=True).values) / (
        range_vals
    )
    final_probabilities = (density - epsilon) + norm_metrics * (2 * epsilon)

    mask = torch.bernoulli(final_probabilities).to(work_dtype)
    res = tensor.to(work_dtype) * mask

    if rescale:
        res = res / (final_probabilities.to(work_dtype))

    return res.squeeze(0)


def sparsify(
    tensor: torch.Tensor,
    density: float,
    method: SparsificationMethod,
    gamma: float = 0,
    rescale: bool = False,
    epsilon: float = 0.15,
) -> torch.Tensor:
    if (
        method == SparsificationMethod.magnitude
        or method == SparsificationMethod.consensus_ties
    ):
        return magnitude(tensor, density=density, rescale=rescale)
    elif method == SparsificationMethod.random:
        return bernoulli(tensor, density=density, rescale=rescale)
    elif method == SparsificationMethod.magnitude_outliers:
        return magnitude_outliers(tensor, density=density, rescale=rescale, gamma=gamma)
    elif method == SparsificationMethod.rank_magnitude_sampling:
        return rank_magnitude(tensor, density=density, rescale=rescale, epsilon=epsilon)
    else:
        raise NotImplementedError(method)


def get_tall_mask(
    delta: torch.Tensor,  # individual task vectors
    lambda_factor: float,  # hyper-parameter lambda for generating TALL masks
    mixed_delta: torch.Tensor,  # multi-task vector
):
    mask = delta.abs() > lambda_factor * (mixed_delta - delta).abs()
    return mask
