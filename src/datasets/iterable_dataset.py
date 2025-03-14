import asyncio
import copy
import inspect
import itertools
import multiprocessing.pool
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from itertools import cycle, islice
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

import fsspec.asyn
import numpy as np
import pandas as pd
import pyarrow as pa

from . import config
from .arrow_dataset import Dataset, DatasetInfoMixin
from .features import Features
from .features.features import (
    FeatureType,
    Value,
    _align_features,
    _check_if_features_can_be_aligned,
    _visit,
    cast_to_python_objects,
)
from .formatting import (
    ArrowFormatter,
    PythonFormatter,
    TableFormatter,
    TensorFormatter,
    get_format_type_from_alias,
    get_formatter,
)
from .info import DatasetInfo
from .splits import NamedSplit, Split
from .table import cast_table_to_features, read_schema_from_file, table_cast
from .utils.logging import get_logger
from .utils.py_utils import Literal
from .utils.sharding import _merge_gen_kwargs, _number_of_shards_in_gen_kwargs, _shuffle_gen_kwargs, _split_gen_kwargs


if TYPE_CHECKING:
    import torch

logger = get_logger(__name__)

Key = Union[int, str]


def identity_func(x):
    return x


def _rename_columns_fn(example: dict, column_mapping: dict[str, str]):
    if any(col not in example for col in column_mapping):
        raise ValueError(
            f"Error when renaming {list(column_mapping)} to {list(column_mapping.values())}: columns {set(column_mapping) - set(example)} are not in the dataset."
        )
    if any(col in example for col in column_mapping.values()):
        raise ValueError(
            f"Error when renaming {list(column_mapping)} to {list(column_mapping.values())}: columns {set(example) - set(column_mapping.values())} are already in the dataset."
        )
    return {
        new_column_name: example[original_column_name]
        for original_column_name, new_column_name in column_mapping.items()
    }


def add_column_fn(example: dict, idx: int, name: str, column: list[dict]):
    if name in example:
        raise ValueError(f"Error when adding {name}: column {name} is already in the dataset.")
    return {name: column[idx]}


def _infer_features_from_batch(batch: dict[str, list], try_features: Optional[Features] = None) -> Features:
    pa_table = pa.Table.from_pydict(batch)
    if try_features is not None:
        try:
            pa_table = table_cast(pa_table, pa.schema(try_features.type))
        except (TypeError, pa.ArrowInvalid, pa.ArrowNotImplementedError):
            pass
    return Features.from_arrow_schema(pa_table.schema)


def _examples_to_batch(examples: list[dict[str, Any]]) -> dict[str, list]:
    # we order the columns by order of appearance
    # to do so, we use a dict as an ordered set
    cols = {col: None for example in examples for col in example}
    # when an example is missing a column, we set the value to None with .get()
    arrays = [[example.get(col) for example in examples] for col in cols]
    return dict(zip(cols, arrays))


def _batch_to_examples(batch: dict[str, list]) -> Iterator[dict[str, Any]]:
    """Convert a batch (dict of examples) to examples list"""
    n_examples = 0 if len(batch) == 0 else len(batch[next(iter(batch))])
    for i in range(n_examples):
        yield {col: array[i] for col, array in batch.items()}


def _convert_to_arrow(
    iterable: Iterable[tuple[Key, dict]],
    batch_size: int,
    drop_last_batch: bool = False,
) -> Iterator[tuple[Key, pa.Table]]:
    """Convert and group examples in Arrow tables of size `batch_size`.

    Args:
        iterable (`Iterable[Tuple[Key, dict]]`):
            An examples iterable containing tuples (example_key, example) of type (int/str, dict)
        batch_size (`Optional[int]`):
            Size of each sub-table to yield. If None or <= 0, yields the full table.
        drop_last_batch (`bool`, defaults to `False`):
            Drop the last batch if it is smaller than `batch_size`.
    """
    if batch_size is None or batch_size <= 0:
        yield (
            "all",
            pa.Table.from_pylist(cast_to_python_objects([example for _, example in iterable], only_1d_for_numpy=True)),
        )
        return
    iterator = iter(iterable)
    for key, example in iterator:
        iterator_batch = islice(iterator, batch_size - 1)
        key_examples_list = [(key, example)] + list(iterator_batch)
        if len(key_examples_list) < batch_size and drop_last_batch:
            return
        keys, examples = zip(*key_examples_list)
        new_key = "_".join(str(key) for key in keys)
        yield new_key, pa.Table.from_pylist(cast_to_python_objects(examples, only_1d_for_numpy=True))


class _BaseExamplesIterable:
    """Base class for the examples iterable used by an IterableDataset"""

    def __init__(self) -> None:
        self._state_dict: Optional[Union[list, dict]] = None

    def __iter__(self) -> Iterator[tuple[Key, dict]]:
        """An examples iterable should yield tuples (example_key, example) of type (int/str, dict)"""
        raise NotImplementedError(f"{type(self)} doesn't implement __iter__ yet")

    @property
    def iter_arrow(self) -> Optional[Callable[[], Iterator[tuple[Key, pa.Table]]]]:
        return None

    @property
    def is_typed(self) -> bool:
        return False

    @property
    def features(self) -> Optional[Features]:
        return None

    def shuffle_data_sources(self, generator: np.random.Generator) -> "_BaseExamplesIterable":
        """
        Either shuffle the shards/sources of the dataset, or propagate the shuffling to the underlying iterable.
        If the order of the shards must stay fixed (when using .skip or .take for example), then this method returns self.
        """
        raise NotImplementedError(f"{type(self)} doesn't implement shuffle_data_sources yet")

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "_BaseExamplesIterable":
        """Either keep only the requested shard, or propagate the request to the underlying iterable."""
        raise NotImplementedError(f"{type(self)} doesn't implement shard_data_sources yet")

    def split_shard_indices_by_worker(self, num_shards: int, index: int, contiguous=True) -> list[int]:
        if contiguous:
            div = self.num_shards // num_shards
            mod = self.num_shards % num_shards
            start = div * index + min(index, mod)
            end = start + div + (1 if index < mod else 0)
            return list(range(start, end))
        else:
            return list(range(index, self.num_shards, num_shards))

    @property
    def num_shards(self) -> int:
        raise NotImplementedError(f"{type(self)} doesn't implement num_shards yet")

    def _init_state_dict(self) -> dict:
        raise NotImplementedError(f"{type(self)} doesn't implement _init_state_dict yet")

    def load_state_dict(self, state_dict: dict) -> dict:
        def _inner_load_state_dict(state, new_state):
            if new_state is not None and isinstance(state, dict):
                for key in new_state:
                    state[key] = _inner_load_state_dict(state[key], new_state[key])
                return state
            elif new_state is not None and isinstance(state, list):
                for i in range(len(state)):
                    state[i] = _inner_load_state_dict(state[i], new_state[i])
                return state
            return new_state

        return _inner_load_state_dict(self._state_dict, state_dict)

    def state_dict(self) -> dict:
        if self._state_dict:
            return copy.deepcopy(self._state_dict)
        raise RuntimeError("State dict is not initialized, please call ex_iterable._init_state_dict() first.")


class ExamplesIterable(_BaseExamplesIterable):
    def __init__(self, generate_examples_fn: Callable[..., tuple[Key, dict]], kwargs: dict):
        super().__init__()
        self.generate_examples_fn = generate_examples_fn
        self.kwargs = kwargs

    def _init_state_dict(self) -> dict:
        self._state_dict = {"shard_idx": 0, "shard_example_idx": 0, "type": self.__class__.__name__}
        return self._state_dict

    def __iter__(self):
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(_split_gen_kwargs(self.kwargs, max_num_jobs=self.num_shards), shard_idx_start, None):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            for key_example in islice(self.generate_examples_fn(**gen_kwags), shard_example_idx_start, None):
                if self._state_dict:
                    self._state_dict["shard_example_idx"] += 1
                yield key_example
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def shuffle_data_sources(self, generator: np.random.Generator) -> "ExamplesIterable":
        return ShuffledDataSourcesExamplesIterable(self.generate_examples_fn, self.kwargs, generator)

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "ExamplesIterable":
        """Keep only the requested shard."""
        gen_kwargs_list = _split_gen_kwargs(self.kwargs, max_num_jobs=self.num_shards)
        shard_indices = self.split_shard_indices_by_worker(num_shards, index, contiguous=contiguous)
        requested_gen_kwargs = _merge_gen_kwargs([gen_kwargs_list[i] for i in shard_indices])
        return ExamplesIterable(self.generate_examples_fn, requested_gen_kwargs)

    @property
    def num_shards(self) -> int:
        return _number_of_shards_in_gen_kwargs(self.kwargs)


class ShuffledDataSourcesExamplesIterable(ExamplesIterable):
    def __init__(
        self, generate_examples_fn: Callable[..., tuple[Key, dict]], kwargs: dict, generator: np.random.Generator
    ):
        super().__init__(generate_examples_fn, kwargs)
        self.generator = deepcopy(generator)

    def _init_state_dict(self) -> dict:
        self._state_dict = {"shard_idx": 0, "shard_example_idx": 0, "type": self.__class__.__name__}
        return self._state_dict

    def __iter__(self):
        """Shuffle the kwargs order to shuffle shards"""
        rng = deepcopy(self.generator)
        kwargs_with_shuffled_shards = _shuffle_gen_kwargs(rng, self.kwargs)
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(
            _split_gen_kwargs(kwargs_with_shuffled_shards, max_num_jobs=self.num_shards), shard_idx_start, None
        ):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            for key_example in islice(self.generate_examples_fn(**gen_kwags), shard_example_idx_start, None):
                if self._state_dict:
                    self._state_dict["shard_example_idx"] += 1
                yield key_example
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "ExamplesIterable":
        """Keep only the requested shard."""
        rng = deepcopy(self.generator)
        kwargs_with_shuffled_shards = _shuffle_gen_kwargs(rng, self.kwargs)
        return ExamplesIterable(self.generate_examples_fn, kwargs_with_shuffled_shards).shard_data_sources(
            num_shards, index, contiguous=contiguous
        )


class ArrowExamplesIterable(_BaseExamplesIterable):
    def __init__(self, generate_tables_fn: Callable[..., tuple[Key, pa.Table]], kwargs: dict):
        super().__init__()
        self.generate_tables_fn = generate_tables_fn
        self.kwargs = kwargs

    @property
    def iter_arrow(self):
        return self._iter_arrow

    def _init_state_dict(self) -> dict:
        self._state_dict = {"shard_idx": 0, "shard_example_idx": 0, "type": self.__class__.__name__}
        return self._state_dict

    def __iter__(self):
        formatter = PythonFormatter()
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(_split_gen_kwargs(self.kwargs, max_num_jobs=self.num_shards), shard_idx_start, None):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            shard_example_idx = 0
            for key, pa_table in self.generate_tables_fn(**gen_kwags):
                if shard_example_idx + len(pa_table) <= shard_example_idx_start:
                    shard_example_idx += len(pa_table)
                    continue
                for pa_subtable in pa_table.to_reader(max_chunksize=config.ARROW_READER_BATCH_SIZE_IN_DATASET_ITER):
                    formatted_batch = formatter.format_batch(pa_subtable)
                    for example in _batch_to_examples(formatted_batch):
                        if shard_example_idx >= shard_example_idx_start:
                            if self._state_dict:
                                self._state_dict["shard_example_idx"] += 1
                            yield key, example
                        shard_example_idx += 1
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def _iter_arrow(self):
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(_split_gen_kwargs(self.kwargs, max_num_jobs=self.num_shards), shard_idx_start, None):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            shard_example_idx = 0
            for key, pa_table in self.generate_tables_fn(**gen_kwags):
                shard_example_idx += len(pa_table)
                if shard_example_idx <= shard_example_idx_start:
                    continue
                if self._state_dict:
                    self._state_dict["shard_example_idx"] += len(pa_table)
                yield key, pa_table
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def shuffle_data_sources(self, generator: np.random.Generator) -> "ArrowExamplesIterable":
        return ShuffledDataSourcesArrowExamplesIterable(self.generate_tables_fn, self.kwargs, generator)

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "ArrowExamplesIterable":
        """Keep only the requested shard."""
        gen_kwargs_list = _split_gen_kwargs(self.kwargs, max_num_jobs=self.num_shards)
        shard_indices = self.split_shard_indices_by_worker(num_shards, index, contiguous=contiguous)
        requested_gen_kwargs = _merge_gen_kwargs([gen_kwargs_list[i] for i in shard_indices])
        return ArrowExamplesIterable(self.generate_tables_fn, requested_gen_kwargs)

    @property
    def num_shards(self) -> int:
        return _number_of_shards_in_gen_kwargs(self.kwargs)


class ShuffledDataSourcesArrowExamplesIterable(ArrowExamplesIterable):
    def __init__(
        self,
        generate_tables_fn: Callable[..., tuple[Key, pa.Table]],
        kwargs: dict,
        generator: np.random.Generator,
    ):
        super().__init__(generate_tables_fn, kwargs)
        self.generator = deepcopy(generator)

    def _init_state_dict(self) -> dict:
        self._state_dict = {"shard_idx": 0, "shard_example_idx": 0, "type": self.__class__.__name__}
        return self._state_dict

    def __iter__(self):
        """Shuffle the kwargs order to shuffle shards"""
        rng = deepcopy(self.generator)
        kwargs_with_shuffled_shards = _shuffle_gen_kwargs(rng, self.kwargs)
        formatter = PythonFormatter()
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(
            _split_gen_kwargs(kwargs_with_shuffled_shards, max_num_jobs=self.num_shards), shard_idx_start, None
        ):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            shard_example_idx = 0
            for key, pa_table in self.generate_tables_fn(**gen_kwags):
                if shard_example_idx + len(pa_table) <= shard_example_idx_start:
                    shard_example_idx += len(pa_table)
                    continue
                for pa_subtable in pa_table.to_reader(max_chunksize=config.ARROW_READER_BATCH_SIZE_IN_DATASET_ITER):
                    formatted_batch = formatter.format_batch(pa_subtable)
                    for example in _batch_to_examples(formatted_batch):
                        if shard_example_idx >= shard_example_idx_start:
                            if self._state_dict:
                                self._state_dict["shard_example_idx"] += 1
                            yield key, example
                        shard_example_idx += 1
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def _iter_arrow(self):
        rng = deepcopy(self.generator)
        kwargs_with_shuffled_shards = _shuffle_gen_kwargs(rng, self.kwargs)
        shard_idx_start = self._state_dict["shard_idx"] if self._state_dict else 0
        for gen_kwags in islice(
            _split_gen_kwargs(kwargs_with_shuffled_shards, max_num_jobs=self.num_shards), shard_idx_start, None
        ):
            shard_example_idx_start = self._state_dict["shard_example_idx"] if self._state_dict else 0
            shard_example_idx = 0
            for key, pa_table in self.generate_tables_fn(**gen_kwags):
                shard_example_idx += len(pa_table)
                if shard_example_idx <= shard_example_idx_start:
                    continue
                if self._state_dict:
                    self._state_dict["shard_example_idx"] += len(pa_table)
                yield key, pa_table
            if self._state_dict:
                self._state_dict["shard_idx"] += 1
                self._state_dict["shard_example_idx"] = 0

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "ArrowExamplesIterable":
        """Keep only the requested shard."""
        rng = deepcopy(self.generator)
        kwargs_with_shuffled_shards = _shuffle_gen_kwargs(rng, self.kwargs)
        return ArrowExamplesIterable(self.generate_tables_fn, kwargs_with_shuffled_shards).shard_data_sources(
            num_shards, index, contiguous=contiguous
        )


class RebatchedArrowExamplesIterable(_BaseExamplesIterable):
    def __init__(self, ex_iterable: _BaseExamplesIterable, batch_size: Optional[int], drop_last_batch: bool = False):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.batch_size = batch_size
        self.drop_last_batch = drop_last_batch

    @property
    def iter_arrow(self):
        return self._iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "examples_iterable": self.ex_iterable._init_state_dict(),
            "previous_state": None,
            "batch_idx": 0,
            "num_chunks_since_previous_state": 0,
            "cropped_chunk_length": 0,
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        yield from self.ex_iterable

    def _iter_arrow(self) -> Iterator[tuple[Key, pa.Table]]:
        """Iterate over sub-tables of size `batch_size`."""
        if self._state_dict and self._state_dict["previous_state"]:
            self.ex_iterable.load_state_dict(self._state_dict["previous_state"])
        if self.ex_iterable.iter_arrow:
            iterator = self.ex_iterable.iter_arrow()
        else:
            iterator = _convert_to_arrow(self.ex_iterable, batch_size=1)
        if self.batch_size is None or self.batch_size <= 0:
            if self._state_dict and self._state_dict["batch_idx"] > 0:
                return
            all_pa_table = pa.concat_tables([pa_table for _, pa_table in iterator])
            if self._state_dict:
                self._state_dict["batch_idx"] = 1
            yield "all", all_pa_table
            return
        keys_buffer = []
        chunks_buffer = []
        chunks_buffer_size = 0
        num_chunks_to_skip = self._state_dict["num_chunks_since_previous_state"] if self._state_dict else 0
        chunk_length_to_crop = self._state_dict["cropped_chunk_length"] if self._state_dict else 0
        if self._state_dict:
            previous_state = self.ex_iterable.state_dict()
            self._state_dict["previous_state"] = previous_state
        for key, pa_table in iterator:
            for num_chunks_since_previous_state, chunk in enumerate(pa_table.to_reader(max_chunksize=self.batch_size)):
                if num_chunks_to_skip > 1:
                    num_chunks_to_skip -= 1
                    continue
                elif num_chunks_to_skip == 1 and chunk_length_to_crop == 0:
                    num_chunks_to_skip -= 1
                    continue
                elif num_chunks_to_skip == 1 and chunk_length_to_crop > 0:
                    chunk = chunk.slice(chunk_length_to_crop, len(chunk) - chunk_length_to_crop)
                    num_chunks_to_skip = 0
                    chunk_length_to_crop = 0
                if len(chunk) == 0:
                    continue

                if chunks_buffer_size + len(chunk) < self.batch_size:
                    keys_buffer.append(key)
                    chunks_buffer.append(chunk)
                    chunks_buffer_size += len(chunk)
                    continue
                elif chunks_buffer_size + len(chunk) == self.batch_size:
                    keys_buffer.append(key)
                    chunks_buffer.append(chunk)
                    new_key = "_".join(str(_key) for _key in keys_buffer)
                    if self._state_dict:
                        self._state_dict["batch_idx"] += 1
                        self._state_dict["num_chunks_since_previous_state"] += len(chunks_buffer)
                        self._state_dict["cropped_chunk_length"] = 0
                    yield new_key, pa.Table.from_batches(chunks_buffer)
                    keys_buffer = []
                    chunks_buffer = []
                    chunks_buffer_size = 0
                    if self._state_dict:
                        self._state_dict["previous_state"] = previous_state
                        self._state_dict["num_chunks_since_previous_state"] = num_chunks_since_previous_state + 1
                else:
                    cropped_chunk_length = self.batch_size - chunks_buffer_size
                    keys_buffer.append(f"{key}[:{cropped_chunk_length}]")
                    chunks_buffer.append(chunk.slice(0, cropped_chunk_length))
                    new_key = "_".join(str(_key) for _key in keys_buffer)
                    if self._state_dict:
                        self._state_dict["batch_idx"] += 1
                        self._state_dict["num_chunks_since_previous_state"] += len(chunks_buffer)
                        self._state_dict["cropped_chunk_length"] = cropped_chunk_length
                    yield new_key, pa.Table.from_batches(chunks_buffer)
                    keys_buffer = [f"{key}[{cropped_chunk_length}:]"]
                    chunks_buffer = [chunk.slice(cropped_chunk_length, len(chunk) - cropped_chunk_length)]
                    chunks_buffer_size = len(chunk) - cropped_chunk_length
                    if self._state_dict:
                        self._state_dict["previous_state"] = previous_state
                        self._state_dict["num_chunks_since_previous_state"] = num_chunks_since_previous_state
            if self._state_dict:
                previous_state = self.ex_iterable.state_dict()
        if not self.drop_last_batch and chunks_buffer:
            new_key = "_".join(str(_key) for _key in keys_buffer)
            if self._state_dict:
                self._state_dict["previous_state"] = previous_state
                self._state_dict["batch_idx"] += 1
                self._state_dict["num_chunks_since_previous_state"] = 0
                self._state_dict["cropped_chunk_length"] = 0
            yield new_key, pa.Table.from_batches(chunks_buffer)

    def shuffle_data_sources(self, generator: np.random.Generator) -> "RebatchedArrowExamplesIterable":
        return RebatchedArrowExamplesIterable(
            self.ex_iterable.shuffle_data_sources(generator), self.batch_size, self.drop_last_batch
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "RebatchedArrowExamplesIterable":
        return RebatchedArrowExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            self.batch_size,
            self.drop_last_batch,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class SelectColumnsIterable(_BaseExamplesIterable):
    def __init__(self, ex_iterable: _BaseExamplesIterable, column_names: list[str]):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.column_names = column_names

    @property
    def iter_arrow(self):
        if self.ex_iterable.iter_arrow:
            return self._iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = self.ex_iterable._init_state_dict()
        return self._state_dict

    def __iter__(self):
        for idx, row in self.ex_iterable:
            yield idx, {c: row[c] for c in self.column_names}

    def _iter_arrow(self) -> Iterator[tuple[Key, pa.Table]]:
        for idx, pa_table in self.ex_iterable.iter_arrow():
            if len(pa_table) > 0:  # empty tables have no schema
                yield idx, pa_table.select(self.column_names)

    def shuffle_data_sources(self, generator: np.random.Generator) -> "SelectColumnsIterable":
        return SelectColumnsIterable(self.ex_iterable.shuffle_data_sources(generator), self.column_names)

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "SelectColumnsIterable":
        return SelectColumnsIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous), self.column_names
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class StepExamplesIterable(_BaseExamplesIterable):
    def __init__(self, ex_iterable: _BaseExamplesIterable, step: int, offset: int):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.step = step
        self.offset = offset
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = self.ex_iterable._init_state_dict()
        return self._state_dict

    def __iter__(self):
        ex_iterator = iter(self.ex_iterable)
        while True:
            batch = list(islice(ex_iterator, self.step))
            if len(batch) > self.offset:
                yield batch[self.offset]
            else:
                break

    def shuffle_data_sources(self, generator: np.random.Generator) -> "StepExamplesIterable":
        return StepExamplesIterable(
            self.ex_iterable.shuffle_data_sources(generator), step=self.step, offset=self.offset
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "StepExamplesIterable":
        return StepExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            step=self.step,
            offset=self.offset,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class CyclingMultiSourcesExamplesIterable(_BaseExamplesIterable):
    def __init__(
        self,
        ex_iterables: list[_BaseExamplesIterable],
        stopping_strategy: Literal["first_exhausted", "all_exhausted"] = "first_exhausted",
    ):
        super().__init__()
        self.ex_iterables = ex_iterables
        self.stopping_strategy = stopping_strategy

        # if undersampling ("first_exhausted"), we stop as soon as one dataset is exhausted
        # if oversampling ("all_exhausted"), we stop as soons as every dataset is exhausted, i.e as soon as every samples of every dataset has been visited at least once
        self.bool_strategy_func = np.all if (stopping_strategy == "all_exhausted") else np.any
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterables[0].is_typed

    @property
    def features(self):
        return self.ex_iterables[0].features

    def _get_indices_iterator(self):
        # this is an infinite iterator to keep track of which iterator we want to pick examples from
        ex_iterable_idx = self._state_dict["ex_iterable_idx"] if self._state_dict else 0
        for next_ex_iterable_idx in islice(cycle(range(len(self.ex_iterables))), ex_iterable_idx + 1, None):
            if self._state_dict:
                self._state_dict["ex_iterable_idx"] = next_ex_iterable_idx
            yield ex_iterable_idx
            ex_iterable_idx = next_ex_iterable_idx

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "ex_iterable_idx": 0,
            "ex_iterables": [ex_iterable._init_state_dict() for ex_iterable in self.ex_iterables],
            "previous_states": [None] * len(self.ex_iterables),
            "is_exhausted": [False] * len(self.ex_iterables),
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        # we use this to buffer one example of each iterator to know if an iterator is exhausted
        nexts = [None] * len(self.ex_iterables)
        # because of that, we need to rewind 1 example when reloading the state dict
        if self._state_dict:
            for i in range(len(self.ex_iterables)):
                if self._state_dict["previous_states"][i] is not None:
                    self.ex_iterables[i].load_state_dict(self._state_dict["previous_states"][i])
        iterators = [iter(ex_iterable) for ex_iterable in self.ex_iterables]

        indices_iterator = self._get_indices_iterator()

        is_exhausted = (
            np.array(self._state_dict["is_exhausted"]) if self._state_dict else np.full(len(self.ex_iterables), False)
        )
        for i in indices_iterator:
            # if the stopping criteria is met, break the main for loop
            if self.bool_strategy_func(is_exhausted):
                break
            # let's pick one example from the iterator at index i
            if nexts[i] is None:
                nexts[i] = next(iterators[i], False)
            result = nexts[i]
            if self._state_dict:
                self._state_dict["previous_states"][i] = deepcopy(self._state_dict["ex_iterables"][i])
            nexts[i] = next(iterators[i], False)

            # the iterator is exhausted
            if nexts[i] is False:
                is_exhausted[i] = True
                if self._state_dict:
                    self._state_dict["is_exhausted"][i] = True
                # we reset it in case the stopping crtieria isn't met yet
                nexts[i] = None
                if self._state_dict:
                    self._state_dict["ex_iterables"][i] = self.ex_iterables[i]._init_state_dict()
                    self._state_dict["previous_states"][i] = None
                iterators[i] = iter(self.ex_iterables[i])

            if result is not False:
                yield result

    def shuffle_data_sources(self, generator: np.random.Generator) -> "CyclingMultiSourcesExamplesIterable":
        """Shuffle each underlying examples iterable."""
        ex_iterables = [ex_iterable.shuffle_data_sources(generator) for ex_iterable in self.ex_iterables]
        return CyclingMultiSourcesExamplesIterable(ex_iterables, self.stopping_strategy)

    @property
    def num_shards(self) -> int:
        return min(ex_iterable.num_shards for ex_iterable in self.ex_iterables)

    def shard_data_sources(
        self, num_shards: int, index: int, contiguous=True
    ) -> "CyclingMultiSourcesExamplesIterable":
        """Either keep only the requested shard, or propagate the request to the underlying iterable."""
        return CyclingMultiSourcesExamplesIterable(
            [iterable.shard_data_sources(num_shards, index, contiguous=contiguous) for iterable in self.ex_iterables],
            stopping_strategy=self.stopping_strategy,
        )


class VerticallyConcatenatedMultiSourcesExamplesIterable(_BaseExamplesIterable):
    """
    VerticallyConcatenatedMultiSourcesExamplesIterable simply chains the input iterables.
    It doesn't require the examples iterables to always yield the same columns.
    Instead, this is handled by the `IterableDataset` class or `FormattedExamplesIterable`.

    For information, `IterableDataset` merges the features of all the datasets to concatenate into one.
    We use `IterableDataset._resolve_features` to obtain the features of all the datasets to concatenate.

    Then for each example, `IterableDataset` and `FormattedExamplesIterable` automatically fill missing columns with None.
    This is done with `_apply_feature_types_on_example`.
    """

    def __init__(self, ex_iterables: list[_BaseExamplesIterable]):
        super().__init__()
        self.ex_iterables = ex_iterables

    @property
    def is_typed(self):
        return self.ex_iterables[0].is_typed

    @property
    def features(self):
        return self.ex_iterables[0].features

    @property
    def iter_arrow(self):
        if all(ex_iterable.iter_arrow is not None for ex_iterable in self.ex_iterables):
            return self._iter_arrow

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "ex_iterable_idx": 0,
            "ex_iterables": [ex_iterable._init_state_dict() for ex_iterable in self.ex_iterables],
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        ex_iterable_idx_start = self._state_dict["ex_iterable_idx"] if self._state_dict else 0
        for ex_iterable in islice(self.ex_iterables, ex_iterable_idx_start, None):
            yield from ex_iterable
            if self._state_dict:
                self._state_dict["ex_iterable_idx"] += 1

    def _iter_arrow(self):
        ex_iterable_idx_start = self._state_dict["ex_iterable_idx"] if self._state_dict else 0
        for ex_iterable in islice(self.ex_iterables, ex_iterable_idx_start, None):
            yield from ex_iterable.iter_arrow()
            if self._state_dict:
                self._state_dict["ex_iterable_idx"] += 1

    def shuffle_data_sources(
        self, generator: np.random.Generator
    ) -> "VerticallyConcatenatedMultiSourcesExamplesIterable":
        """Shuffle the list of examples iterable, as well as each underlying examples iterable."""
        rng = deepcopy(generator)
        ex_iterables = list(self.ex_iterables)
        rng.shuffle(ex_iterables)
        ex_iterables = [ex_iterable.shuffle_data_sources(generator) for ex_iterable in ex_iterables]
        return VerticallyConcatenatedMultiSourcesExamplesIterable(ex_iterables)

    @property
    def num_shards(self) -> int:
        return min(ex_iterable.num_shards for ex_iterable in self.ex_iterables)

    def shard_data_sources(
        self, num_shards: int, index: int, contiguous=True
    ) -> "VerticallyConcatenatedMultiSourcesExamplesIterable":
        """Either keep only the requested shard, or propagate the request to the underlying iterable."""
        return VerticallyConcatenatedMultiSourcesExamplesIterable(
            [iterable.shard_data_sources(num_shards, index, contiguous=contiguous) for iterable in self.ex_iterables]
        )


def _check_column_names(column_names: list[str]):
    """Check the column names to make sure they don't contain duplicates."""
    counter = Counter(column_names)
    if not all(count == 1 for count in counter.values()):
        duplicated_columns = [col for col in counter if counter[col] > 1]
        raise ValueError(
            f"The examples iterables can't have duplicated columns but columns {duplicated_columns} are duplicated."
        )


class HorizontallyConcatenatedMultiSourcesExamplesIterable(_BaseExamplesIterable):
    """
    HorizontallyConcatenatedMultiSourcesExamplesIterable merges examples together for the input list of iterables.
    It also checks that there are no duplicate columns (otherwise we don't know which one to keep).
    This check is done once when yielding the first example.

    However it doesn't fill missing columns with None.
    Instead, this is handled by the `IterableDataset` class or `FormattedExamplesIterable`.

    For information, `IterableDataset` merges the features of all the datasets to concatenate into one.
    We use `IterableDataset._resolve_features` to obtain the features of all the datasets to concatenate.

    Then for each example, `IterableDataset` and `FormattedExamplesIterable` automatically fill missing columns with None.
    This is done with `_apply_feature_types_on_example`.
    """

    def __init__(self, ex_iterables: list[_BaseExamplesIterable]):
        super().__init__()
        self.ex_iterables = ex_iterables
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterables[0].is_typed

    @property
    def features(self):
        return self.ex_iterables[0].features

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "ex_iterables": [ex_iterable._init_state_dict() for ex_iterable in self.ex_iterables],
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        ex_iterators = [iter(ex_iterable) for ex_iterable in self.ex_iterables]
        for i in itertools.count():
            keys = []
            examples = []
            for ex_iterator in list(ex_iterators):
                try:
                    key, example = next(ex_iterator)
                    keys.append(key)
                    examples.append(example)
                except StopIteration:
                    ex_iterators.remove(ex_iterator)
            if ex_iterators:
                if i == 0:
                    _check_column_names([column_name for example in examples for column_name in example])
                new_example = {}
                for example in examples:
                    new_example.update(example)
                new_key = "_".join(str(key) for key in keys)
                yield new_key, new_example
            else:
                break

    def shuffle_data_sources(
        self, generator: np.random.Generator
    ) -> "HorizontallyConcatenatedMultiSourcesExamplesIterable":
        """Doesn't shuffle the wrapped examples iterable since it would break the alignment between them."""
        return self

    @property
    def num_shards(self) -> int:
        return 1

    def shard_data_sources(
        self, num_shards: int, index: int, contiguous=True
    ) -> "HorizontallyConcatenatedMultiSourcesExamplesIterable":
        """Either keep only the requested shard, or propagate the request to the underlying iterable."""
        return HorizontallyConcatenatedMultiSourcesExamplesIterable(
            [iterable.shard_data_sources(num_shards, index, contiguous=contiguous) for iterable in self.ex_iterables]
        )


class RandomlyCyclingMultiSourcesExamplesIterable(CyclingMultiSourcesExamplesIterable):
    def __init__(
        self,
        ex_iterables: list[_BaseExamplesIterable],
        generator: np.random.Generator,
        probabilities: Optional[list[float]] = None,
        stopping_strategy: Literal["first_exhausted", "all_exhausted"] = "first_exhausted",
    ):
        super().__init__(ex_iterables, stopping_strategy)
        self.generator = deepcopy(generator)
        self.probabilities = probabilities
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterables[0].is_typed

    @property
    def features(self):
        return self.ex_iterables[0].features

    def _get_indices_iterator(self):
        rng = deepcopy(self.generator)
        num_sources = len(self.ex_iterables)
        random_batch_size = 1000
        # this is an infinite iterator that randomly samples the index of the source to pick examples from
        index_offset = self._state_dict["bit_generator_index_offset"] if self._state_dict else 0
        if self._state_dict:
            rng.bit_generator.state = self._state_dict["bit_generator_state"]
        if self.probabilities is None:
            while True:
                for i in islice(rng.integers(0, num_sources, size=random_batch_size), index_offset, None):
                    index_offset = (index_offset + 1) % random_batch_size
                    if self._state_dict:
                        self._state_dict["bit_generator_index_offset"] = index_offset
                        if index_offset == 0:
                            self._state_dict["bit_generator_state"] = rng.bit_generator.state
                    yield int(i)
        else:
            while True:
                for i in islice(
                    rng.choice(num_sources, size=random_batch_size, p=self.probabilities), index_offset, None
                ):
                    index_offset = (index_offset + 1) % random_batch_size
                    if self._state_dict:
                        self._state_dict["bit_generator_index_offset"] = index_offset
                        if index_offset == 0:
                            self._state_dict["bit_generator_state"] = rng.bit_generator.state
                    yield int(i)

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "bit_generator_state": self.generator.bit_generator.state,
            "bit_generator_index_offset": 0,
            "ex_iterables": [ex_iterable._init_state_dict() for ex_iterable in self.ex_iterables],
            "previous_states": [None] * len(self.ex_iterables),
            "is_exhausted": [False] * len(self.ex_iterables),
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def shuffle_data_sources(self, generator: np.random.Generator) -> "RandomlyCyclingMultiSourcesExamplesIterable":
        """Shuffle the data sources of each wrapped examples iterable."""
        ex_iterables = [ex_iterable.shuffle_data_sources(generator) for ex_iterable in self.ex_iterables]
        return RandomlyCyclingMultiSourcesExamplesIterable(
            ex_iterables,
            generator=generator,
            probabilities=self.probabilities,
            stopping_strategy=self.stopping_strategy,
        )

    def shard_data_sources(
        self, num_shards: int, index: int, contiguous=True
    ) -> "RandomlyCyclingMultiSourcesExamplesIterable":
        """Either keep only the requested shard, or propagate the request to the underlying iterable."""
        return RandomlyCyclingMultiSourcesExamplesIterable(
            [iterable.shard_data_sources(num_shards, index, contiguous=contiguous) for iterable in self.ex_iterables],
            self.generator,
            self.probabilities,
            self.stopping_strategy,
        )


def _table_output_to_arrow(output) -> pa.Table:
    if isinstance(output, pa.Table):
        return output
    if isinstance(output, (pd.DataFrame, pd.Series)):
        return pa.Table.from_pandas(output)
    if config.POLARS_AVAILABLE and "polars" in sys.modules:
        import polars as pl

        if isinstance(output, (pl.DataFrame, pl.Series)):
            return output.to_arrow()
    return output


class MappedExamplesIterable(_BaseExamplesIterable):
    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        function: Callable,
        with_indices: bool = False,
        input_columns: Optional[list[str]] = None,
        batched: bool = False,
        batch_size: Optional[int] = 1000,
        drop_last_batch: bool = False,
        remove_columns: Optional[list[str]] = None,
        fn_kwargs: Optional[dict] = None,
        formatting: Optional["FormattingConfig"] = None,
        features: Optional[Features] = None,
        max_num_running_async_map_functions_in_parallel: Optional[int] = None,
    ):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.function = function
        self.batched = batched
        self.batch_size = batch_size
        self.drop_last_batch = drop_last_batch
        self.remove_columns = remove_columns
        self.with_indices = with_indices
        self.input_columns = input_columns
        self.fn_kwargs = fn_kwargs or {}
        self.formatting = formatting  # required for iter_arrow
        self._features = features
        self.max_num_running_async_map_functions_in_parallel = (
            max_num_running_async_map_functions_in_parallel or config.MAX_NUM_RUNNING_ASYNC_MAP_FUNCTIONS_IN_PARALLEL
        )
        # sanity checks
        if formatting and formatting.is_table:
            # batch_size should match for iter_arrow
            if not isinstance(ex_iterable, RebatchedArrowExamplesIterable):
                raise ValueError(
                    f"The {formatting.format_type.capitalize()}-formatted {type(self).__name__} has underlying iterable"
                    f"that is a {type(ex_iterable).__name__} instead of a RebatchedArrowExamplesIterable."
                )
            elif ex_iterable.batch_size != (batch_size if batched else 1):
                raise ValueError(
                    f"The {formatting.format_type.capitalize()}-formatted {type(self).__name__} has batch_size={batch_size if batched else 1} which is"
                    f"different from {ex_iterable.batch_size=} from its underlying iterable."
                )
        # to enable graceful ends
        self._owned_loops_and_tasks: list[tuple[asyncio.AbstractEventLoop, list[asyncio.Task]]] = []

    @property
    def iter_arrow(self):
        if self.formatting and self.formatting.is_table:
            return self._iter_arrow

    @property
    def is_typed(self):
        return self.features is not None  # user has extracted features

    @property
    def features(self):
        return self._features

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "examples_iterable": self.ex_iterable._init_state_dict(),
            "previous_state": None,
            "num_examples_since_previous_state": 0,
            "previous_state_example_idx": 0,
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        if self.formatting and self.formatting.is_table:
            formatter = PythonFormatter()
            for key, pa_table in self._iter_arrow(max_chunksize=1):
                yield key, formatter.format_row(pa_table)
        else:
            yield from self._iter()

    def _iter(self):
        current_idx = self._state_dict["previous_state_example_idx"] if self._state_dict else 0
        if self._state_dict and self._state_dict["previous_state"]:
            self.ex_iterable.load_state_dict(self._state_dict["previous_state"])
            num_examples_to_skip = self._state_dict["num_examples_since_previous_state"]
        else:
            num_examples_to_skip = 0
        iterator = iter(self.ex_iterable)

        # We use the same logic as in Dataset.map, but with less features/formatting
        # since they're handled by FormattedExamplesIterable

        if self.formatting:
            formatter = get_formatter(self.formatting.format_type)
            format_dict = formatter.recursive_tensorize if isinstance(formatter, TensorFormatter) else None
        else:
            format_dict = None

        def iter_batched_inputs():
            nonlocal current_idx
            for key, example in iterator:
                # If `batched`, first build the batch, if `batch_size` is None or <=0, then the batch is the whole dataset
                iterator_batch = (
                    iterator
                    if self.batch_size is None or self.batch_size <= 0
                    else islice(iterator, self.batch_size - 1)
                )
                key_examples_list = [(key, example)] + list(iterator_batch)
                keys, examples = zip(*key_examples_list)
                # the new key is the concatenation of the examples keys from the batch
                key = "_".join(str(key) for key in keys)
                if (
                    self.drop_last_batch
                    and self.batch_size is not None
                    and self.batch_size > 0
                    and len(examples) < self.batch_size
                ):  # ignore last batch
                    return
                batch = _examples_to_batch(examples)
                # we need to format here in case we need to stack tensors together
                batch = format_dict(batch) if format_dict else batch
                indices = [current_idx + i for i in range(len(key_examples_list))]
                current_idx += len(indices)
                yield indices, (key, batch)

        def iter_inputs():
            nonlocal current_idx
            for key, example in iterator:
                # If not batched, we can apply the transform and yield the example directly
                # first copy the example, since we might drop some keys
                example = dict(example)
                # no need to do formatting here
                current_idx += 1
                yield current_idx - 1, (key, example)

        def validate_function_output(processed_inputs):
            if self.batched and processed_inputs:
                first_col = next(iter(processed_inputs))
                bad_cols = [
                    col for col in processed_inputs if len(processed_inputs[col]) != len(processed_inputs[first_col])
                ]
                if bad_cols:
                    raise ValueError(
                        f"Column lengths mismatch: columns {bad_cols} have length {[len(processed_inputs[col]) for col in bad_cols]} "
                        f"while {first_col} has length {len(processed_inputs[first_col])}."
                    )

        def prepare_inputs(key_example, indices):
            key, example = key_example
            fn_args = [example] if self.input_columns is None else [example[col] for col in self.input_columns]
            additional_args = ()
            if self.with_indices:
                fn_args += (indices,)
            inputs = dict(example)
            return inputs, fn_args, additional_args, self.fn_kwargs

        def prepare_outputs(key_example, inputs, processed_inputs):
            validate_function_output(processed_inputs)
            # this logic mimics the one in Dataset.map
            if self.remove_columns:
                for c in self.remove_columns:
                    if c in inputs:
                        del inputs[c]
                    if processed_inputs is key_example[1] and c in processed_inputs:
                        del processed_inputs[c]
            transformed_inputs = {**inputs, **processed_inputs}
            # no need to do features decoding here
            return transformed_inputs

        def apply_function(key_example, indices):
            """Utility to apply the function on a selection of columns."""
            inputs, fn_args, additional_args, fn_kwargs = prepare_inputs(key_example, indices)
            processed_inputs = self.function(*fn_args, *additional_args, **fn_kwargs)
            return prepare_outputs(key_example, inputs, processed_inputs)

        async def async_apply_function(key_example, indices):
            """Utility to apply the function on a selection of columns. Same code but async"""
            inputs, fn_args, additional_args, fn_kwargs = prepare_inputs(key_example, indices)
            processed_inputs = await self.function(*fn_args, *additional_args, **fn_kwargs)
            return prepare_outputs(key_example, inputs, processed_inputs)

        tasks: list[asyncio.Task] = []
        if inspect.iscoroutinefunction(self.function):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            self._owned_loops_and_tasks.append((loop, tasks))
        else:
            loop = None

        def iter_outputs():
            nonlocal tasks, loop
            inputs_iterator = iter_batched_inputs() if self.batched else iter_inputs()
            if inspect.iscoroutinefunction(self.function):
                if self._state_dict:
                    previous_state = self.ex_iterable.state_dict()
                    self._state_dict["previous_state"] = previous_state
                    previous_state_task = None
                    previous_state_example_idx = self._state_dict["previous_state_example_idx"]
                indices: Union[list[int], list[list[int]]] = []
                for i, key_example in inputs_iterator:
                    indices.append(i)
                    tasks.append(loop.create_task(async_apply_function(key_example, i)))
                    # keep the total active tasks under a certain number
                    if len(tasks) >= self.max_num_running_async_map_functions_in_parallel:
                        done, pending = loop.run_until_complete(
                            asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        )
                        while tasks and len(pending) >= self.max_num_running_async_map_functions_in_parallel:
                            done, pending = loop.run_until_complete(
                                asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            )
                    if len(tasks) >= 10 * self.max_num_running_async_map_functions_in_parallel:
                        loop.run_until_complete(tasks[0])
                    # yield finished tasks
                    while tasks and tasks[0].done():
                        i, task = indices.pop(0), tasks.pop(0)
                        yield i, task.result()
                        if self._state_dict and task is previous_state_task:
                            self._state_dict["previous_state"] = previous_state
                            self._state_dict["num_examples_since_previous_state"] = 0
                            self._state_dict["previous_state_example_idx"] = previous_state_example_idx
                            previous_state, previous_state_task = None, None
                    # checkpoint
                    if self._state_dict and previous_state_task is None and tasks:
                        previous_state = self.ex_iterable.state_dict()
                        previous_state_task = tasks[-1]
                        previous_state_example_idx = current_idx
                while tasks:
                    yield indices[0], loop.run_until_complete(tasks[0])
                    indices.pop(0), tasks.pop(0)
            else:
                if self._state_dict:
                    if self.batched:
                        self._state_dict["previous_state"] = self.ex_iterable.state_dict()
                        self._state_dict["num_examples_since_previous_state"] = 0
                        self._state_dict["previous_state_example_idx"] = current_idx
                for i, key_example in inputs_iterator:
                    if self._state_dict:
                        if not self.batched:
                            self._state_dict["previous_state_example_idx"] = current_idx
                    yield i, apply_function(key_example, i)
                    if self._state_dict:
                        if self.batched:
                            self._state_dict["previous_state"] = self.ex_iterable.state_dict()
                            self._state_dict["num_examples_since_previous_state"] = 0
                            self._state_dict["previous_state_example_idx"] = current_idx

        try:
            outputs = iter_outputs()
            if self.batched:
                outputs = (
                    (key, transformed_example)
                    for key, transformed_batch in outputs
                    for transformed_example in _batch_to_examples(transformed_batch)
                )
            for key, transformed_example in outputs:
                if self._state_dict and self._state_dict["previous_state"] is not None:
                    self._state_dict["num_examples_since_previous_state"] += 1
                if num_examples_to_skip > 0:
                    num_examples_to_skip -= 1
                    continue
                yield key, transformed_example
        except (Exception, KeyboardInterrupt):
            if loop:
                logger.debug(f"Canceling {len(tasks)} async tasks.")
                for task in tasks:
                    task.cancel(msg="KeyboardInterrupt")
                try:
                    loop.run_until_complete(asyncio.gather(*tasks))
                except (asyncio.CancelledError, ValueError):
                    logger.debug("Tasks canceled.")
            raise

    def _iter_arrow(self, max_chunksize: Optional[int] = None) -> Iterator[tuple[Key, pa.Table]]:
        formatter: TableFormatter = get_formatter(self.formatting.format_type) if self.formatting else ArrowFormatter()
        if self.ex_iterable.iter_arrow:
            iterator = self.ex_iterable.iter_arrow()
        else:
            iterator = _convert_to_arrow(
                self.ex_iterable,
                batch_size=self.batch_size if self.batched else 1,
                drop_last_batch=self.drop_last_batch,
            )
        if self._state_dict and self._state_dict["previous_state"]:
            self.ex_iterable.load_state_dict(self._state_dict["previous_state"])
            num_examples_to_skip = self._state_dict["num_examples_since_previous_state"]
        else:
            num_examples_to_skip = 0
        if self._state_dict and max_chunksize is not None:
            self._state_dict["previous_state"] = self.ex_iterable.state_dict()
            self._state_dict["num_examples_since_previous_state"] = 0
        current_idx = self._state_dict["previous_state_example_idx"] if self._state_dict else 0
        for key, pa_table in iterator:
            if (
                self.batched
                and self.batch_size is not None
                and len(pa_table) < self.batch_size
                and self.drop_last_batch
            ):
                return
            # first build the batch
            function_args = (
                [formatter.format_batch(pa_table)]
                if self.input_columns is None
                else [pa_table[col] for col in self.input_columns]
            )
            if self.with_indices:
                if self.batched:
                    function_args.append([current_idx + i for i in range(len(pa_table))])
                else:
                    function_args.append(current_idx)
            # then apply the transform
            output = self.function(*function_args, **self.fn_kwargs)
            output_table = _table_output_to_arrow(output)
            if not isinstance(output_table, pa.Table):
                raise TypeError(
                    f"Provided `function` which is applied to {formatter.table_type} returns a variable of type "
                    f"{type(output)}. Make sure provided `function` returns a {formatter.table_type} to update the dataset."
                )
            # we don't need to merge results for consistency with Dataset.map which merges iif both input and output are dicts
            # then remove the unwanted columns
            if self.remove_columns:
                for column in self.remove_columns:
                    if column in output_table.column_names:
                        output_table = output_table.remove_column(output_table.column_names.index(column))
            # return output
            if max_chunksize is None:
                current_idx += len(pa_table)
                if self._state_dict:
                    self._state_dict["previous_state_example_idx"] += len(pa_table)
                yield key, output_table
            else:
                for i, pa_subtable in enumerate(output_table.to_reader(max_chunksize=max_chunksize)):
                    current_idx += 1
                    if self._state_dict:
                        self._state_dict["num_examples_since_previous_state"] += 1
                    if num_examples_to_skip > 0:
                        num_examples_to_skip -= 1
                        continue
                    yield f"{key}_{i}", pa_subtable
                if self._state_dict:
                    self._state_dict["previous_state"] = self.ex_iterable.state_dict()
                    self._state_dict["num_examples_since_previous_state"] = 0
                    self._state_dict["previous_state_example_idx"] += len(pa_table)

    def shuffle_data_sources(self, generator: np.random.Generator) -> "MappedExamplesIterable":
        """Shuffle the wrapped examples iterable."""
        return MappedExamplesIterable(
            self.ex_iterable.shuffle_data_sources(generator),
            function=self.function,
            with_indices=self.with_indices,
            input_columns=self.input_columns,
            batched=self.batched,
            batch_size=self.batch_size,
            drop_last_batch=self.drop_last_batch,
            remove_columns=self.remove_columns,
            fn_kwargs=self.fn_kwargs,
            formatting=self.formatting,
            features=self.features,
            max_num_running_async_map_functions_in_parallel=self.max_num_running_async_map_functions_in_parallel,
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "MappedExamplesIterable":
        """Keep only the requested shard."""
        return MappedExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            function=self.function,
            with_indices=self.with_indices,
            input_columns=self.input_columns,
            batched=self.batched,
            batch_size=self.batch_size,
            drop_last_batch=self.drop_last_batch,
            remove_columns=self.remove_columns,
            fn_kwargs=self.fn_kwargs,
            formatting=self.formatting,
            features=self.features,
            max_num_running_async_map_functions_in_parallel=self.max_num_running_async_map_functions_in_parallel,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


def _add_mask(
    input: Union[dict, pa.Table],
    mask: Union[bool, list, pa.Array, pa.ChunkedArray, pa.BooleanScalar],
    mask_column_name: str,
):
    if isinstance(input, pa.Table):
        if not isinstance(mask, (list, pa.Array, pa.ChunkedArray)):
            mask = pa.array([mask], type=pa.bool_())
        return input.append_column(mask_column_name, mask)
    else:
        return {mask_column_name: mask}


def add_mask(mask_function: Callable, input: Union[dict, pa.Table], *args, mask_column_name: str, **kwargs):
    mask = mask_function(input, *args, **kwargs)
    return _add_mask(input, mask, mask_column_name)


async def async_add_mask(
    mask_function: Callable, input: Union[dict, pa.Table], *args, mask_column_name: str, **kwargs
):
    mask = await mask_function(input, *args, **kwargs)
    return _add_mask(input, mask, mask_column_name)


class FilteredExamplesIterable(MappedExamplesIterable):
    mask_column_name = "===MASK==="

    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        function: Callable,
        with_indices: bool = False,
        input_columns: Optional[list[str]] = None,
        batched: bool = False,
        batch_size: Optional[int] = 1000,
        fn_kwargs: Optional[dict] = None,
        formatting: Optional["FormattingConfig"] = None,
    ):
        self.mask_function = function
        if ex_iterable.is_typed:
            features = Features({**ex_iterable.features, self.mask_column_name: Value("bool")})
        else:
            features = None
        super().__init__(
            ex_iterable=ex_iterable,
            function=partial(
                async_add_mask if inspect.iscoroutinefunction(function) else add_mask,
                function,
                mask_column_name=self.mask_column_name,
            ),
            with_indices=with_indices,
            input_columns=input_columns,
            batched=batched,
            batch_size=batch_size,
            fn_kwargs=fn_kwargs,
            formatting=formatting,
            features=features,
        )

    def _iter(self):
        for key, example in super()._iter():
            example = dict(example)
            if example.pop(self.mask_column_name):
                yield key, example

    def _iter_arrow(self, max_chunksize: Optional[int] = None):
        for key, pa_table in super()._iter_arrow(max_chunksize=max_chunksize):
            mask = pa_table[self.mask_column_name]
            yield key, pa_table.drop(self.mask_column_name).filter(mask)

    def shuffle_data_sources(self, seed: Optional[int]) -> "FilteredExamplesIterable":
        """Shuffle the wrapped examples iterable."""
        return FilteredExamplesIterable(
            self.ex_iterable.shuffle_data_sources(seed),
            function=self.mask_function,
            with_indices=self.with_indices,
            input_columns=self.input_columns,
            batched=self.batched,
            batch_size=self.batch_size,
            fn_kwargs=self.fn_kwargs,
            formatting=self.formatting,
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "FilteredExamplesIterable":
        """Keep only the requested shard."""
        return FilteredExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            function=self.mask_function,
            with_indices=self.with_indices,
            input_columns=self.input_columns,
            batched=self.batched,
            batch_size=self.batch_size,
            fn_kwargs=self.fn_kwargs,
            formatting=self.formatting,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class BufferShuffledExamplesIterable(_BaseExamplesIterable):
    def __init__(self, ex_iterable: _BaseExamplesIterable, buffer_size: int, generator: np.random.Generator):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.buffer_size = buffer_size
        self.generator = generator
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = self.ex_iterable._init_state_dict()
        self._original_state_dict = self.state_dict()
        return self._state_dict

    def load_state_dict(self, state_dict: dict) -> dict:
        if self._state_dict:
            if state_dict != self._original_state_dict:
                logger.warning(
                    "Loading a state dict of a shuffle buffer of a dataset without the buffer content."
                    "The shuffle buffer will be refilled before starting to yield new examples."
                )
        return super().load_state_dict(state_dict)

    @staticmethod
    def _iter_random_indices(rng: np.random.Generator, buffer_size: int, random_batch_size=1000) -> Iterator[int]:
        while True:
            yield from (int(i) for i in rng.integers(0, buffer_size, size=random_batch_size))

    def __iter__(self):
        buffer_size = self.buffer_size
        rng = deepcopy(self.generator)
        indices_iterator = self._iter_random_indices(rng, buffer_size)
        # this is the shuffle buffer that we keep in memory
        mem_buffer = []
        for x in self.ex_iterable:
            if len(mem_buffer) == buffer_size:  # if the buffer is full, pick and example from it
                i = next(indices_iterator)
                yield mem_buffer[i]
                mem_buffer[i] = x  # replace the picked example by a new one
            else:  # otherwise, keep filling the buffer
                mem_buffer.append(x)
        # when we run out of examples, we shuffle the remaining examples in the buffer and yield them
        rng.shuffle(mem_buffer)
        yield from mem_buffer

    def shuffle_data_sources(self, generator: np.random.Generator) -> "BufferShuffledExamplesIterable":
        """Shuffle the wrapped examples iterable as well as the shuffling buffer."""
        return BufferShuffledExamplesIterable(
            self.ex_iterable.shuffle_data_sources(generator), buffer_size=self.buffer_size, generator=generator
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "BufferShuffledExamplesIterable":
        """Keep only the requested shard."""
        return BufferShuffledExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            buffer_size=self.buffer_size,
            generator=self.generator,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class SkipExamplesIterable(_BaseExamplesIterable):
    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        n: int,
        block_sources_order_when_shuffling: bool = True,
        split_when_sharding: bool = True,
    ):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.n = n
        self.block_sources_order_when_shuffling = block_sources_order_when_shuffling
        self.split_when_sharding = split_when_sharding
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "skipped": False,
            "examples_iterable": self.ex_iterable._init_state_dict(),
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        ex_iterable_idx_start = 0 if self._state_dict and self._state_dict["skipped"] else self.n
        if self._state_dict:
            self._state_dict["skipped"] = True
        yield from islice(self.ex_iterable, ex_iterable_idx_start, None)

    @staticmethod
    def split_number(num, n):
        quotient = num // n
        remainder = num % n
        result = [quotient] * n
        for i in range(remainder):
            result[i] += 1
        return result

    def shuffle_data_sources(self, generator: np.random.Generator) -> "SkipExamplesIterable":
        """May not shuffle the wrapped examples iterable since it would skip examples from other shards instead."""
        if self.block_sources_order_when_shuffling:
            return self
        else:
            return SkipExamplesIterable(
                self.ex_iterable.shuffle_data_sources(generator),
                n=self.n,
                block_sources_order_when_shuffling=self.block_sources_order_when_shuffling,
                split_when_sharding=self.split_when_sharding,
            )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "SkipExamplesIterable":
        """Keep only the requested shard."""
        if self.split_when_sharding:
            return SkipExamplesIterable(
                self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
                n=self.split_number(self.n, num_shards)[index],
                block_sources_order_when_shuffling=self.block_sources_order_when_shuffling,
                split_when_sharding=self.split_when_sharding,
            )
        else:
            return self

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


class RepeatExamplesIterable(_BaseExamplesIterable):
    """
    Iterable that repeats the underlying iterable a given number of times.
    """

    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        num_times: Optional[int],
    ):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.num_times = num_times

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "repeat_index": 0,
            "examples_iterable": self.ex_iterable._init_state_dict(),
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        repeat_index = self._state_dict["repeat_index"] if self._state_dict else 0
        while True:
            if self.num_times is not None and repeat_index >= max(self.num_times, 0):
                break
            yield from self.ex_iterable
            repeat_index += 1
            if self._state_dict:
                self._state_dict["repeat_index"] = repeat_index
                self._state_dict["examples_iterable"] = self.ex_iterable._init_state_dict()

    def shuffle_data_sources(self, generator: np.random.Generator) -> "RepeatExamplesIterable":
        """Shuffle the underlying iterable, then repeat."""
        return RepeatExamplesIterable(self.ex_iterable.shuffle_data_sources(generator), num_times=self.num_times)

    def shard_data_sources(self, worker_id: int, num_workers: int) -> "RepeatExamplesIterable":
        """Shard, then repeat shards."""
        return RepeatExamplesIterable(
            self.ex_iterable.shard_data_sources(worker_id, num_workers),
            num_times=self.num_times,
        )

    @property
    def n_shards(self) -> int:
        return self.ex_iterable.n_shards


class TakeExamplesIterable(_BaseExamplesIterable):
    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        n: int,
        block_sources_order_when_shuffling: bool = True,
        split_when_sharding: bool = True,
    ):
        super().__init__()
        self.ex_iterable = ex_iterable
        self.n = n
        self.block_sources_order_when_shuffling = block_sources_order_when_shuffling
        self.split_when_sharding = split_when_sharding
        # TODO(QL): implement iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed

    @property
    def features(self):
        return self.ex_iterable.features

    def _init_state_dict(self) -> dict:
        self._state_dict = {
            "num_taken": 0,
            "examples_iterable": self.ex_iterable._init_state_dict(),
            "type": self.__class__.__name__,
        }
        return self._state_dict

    def __iter__(self):
        ex_iterable_num_taken = self._state_dict["num_taken"] if self._state_dict else 0
        for key_example in islice(self.ex_iterable, self.n - ex_iterable_num_taken):
            if self._state_dict:
                self._state_dict["num_taken"] += 1
            yield key_example

    @staticmethod
    def split_number(num, n):
        quotient = num // n
        remainder = num % n
        result = [quotient] * n
        for i in range(remainder):
            result[i] += 1
        return result

    def shuffle_data_sources(self, generator: np.random.Generator) -> "TakeExamplesIterable":
        """May not shuffle the wrapped examples iterable since it would take examples from other shards instead."""
        if self.block_sources_order_when_shuffling:
            return self
        else:
            return TakeExamplesIterable(
                self.ex_iterable.shuffle_data_sources(generator),
                n=self.n,
                block_sources_order_when_shuffling=self.block_sources_order_when_shuffling,
                split_when_sharding=self.split_when_sharding,
            )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "TakeExamplesIterable":
        """Keep only the requested shard."""
        if self.split_when_sharding:
            return TakeExamplesIterable(
                self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
                n=self.split_number(self.n, num_shards)[index],
                block_sources_order_when_shuffling=self.block_sources_order_when_shuffling,
                split_when_sharding=self.split_when_sharding,
            )
        else:
            return TakeExamplesIterable(
                self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
                n=self.n,
                block_sources_order_when_shuffling=self.block_sources_order_when_shuffling,
                split_when_sharding=self.split_when_sharding,
            )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


def _apply_feature_types_on_example(
    example: dict, features: Features, token_per_repo_id: dict[str, Union[str, bool, None]]
) -> dict:
    example = dict(example)
    # add missing columns
    for column_name in features:
        if column_name not in example:
            example[column_name] = None
    # we encode the example for ClassLabel feature types for example
    encoded_example = features.encode_example(example)
    # Decode example for Audio feature, e.g.
    decoded_example = features.decode_example(encoded_example, token_per_repo_id=token_per_repo_id)
    return decoded_example


def _apply_feature_types_on_batch(
    batch: dict, features: Features, token_per_repo_id: dict[str, Union[str, bool, None]]
) -> dict:
    batch = dict(batch)
    # add missing columns
    n_examples = len(batch[next(iter(batch))])
    for column_name in features:
        if column_name not in batch:
            batch[column_name] = [None] * n_examples
    # we encode the batch for ClassLabel feature types for example
    encoded_batch = features.encode_batch(batch)
    # Decode batch for Audio feature, e.g.
    decoded_batch = features.decode_batch(encoded_batch, token_per_repo_id=token_per_repo_id)
    return decoded_batch


@dataclass
class FormattingConfig:
    format_type: Optional[str]

    @property
    def is_table(self) -> bool:
        return isinstance(get_formatter(self.format_type), TableFormatter)

    @property
    def is_tensor(self) -> bool:
        return isinstance(get_formatter(self.format_type), TensorFormatter)


class FormattedExamplesIterable(_BaseExamplesIterable):
    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        formatting: Optional[FormattingConfig],
        features: Optional[Features],
        token_per_repo_id: dict[str, Union[str, bool, None]],
    ):
        super().__init__()
        self.ex_iterable = ex_iterable
        self._features = features
        self.formatting = formatting
        self.token_per_repo_id = token_per_repo_id

    @property
    def iter_arrow(self):
        if self.ex_iterable.iter_arrow and (not self.formatting or self.formatting.is_table):
            return self._iter_arrow

    @property
    def is_typed(self):
        return self.ex_iterable.is_typed or self._features is not None

    @property
    def features(self):
        return self._features

    def _init_state_dict(self) -> dict:
        self._state_dict = self.ex_iterable._init_state_dict()
        return self._state_dict

    def __iter__(self):
        if not self.formatting or self.formatting.is_table:
            formatter = PythonFormatter(features=self._features if not self.ex_iterable.is_typed else None)
        else:
            formatter = get_formatter(
                self.formatting.format_type,
                features=self._features if not self.ex_iterable.is_typed else None,
                token_per_repo_id=self.token_per_repo_id,
            )
        if self.ex_iterable.iter_arrow:
            # feature casting (inc column addition) handled within self._iter_arrow()
            for key, pa_table in self._iter_arrow():
                batch = formatter.format_batch(pa_table)
                for example in _batch_to_examples(batch):
                    yield key, example
        else:
            format_dict = (
                formatter.recursive_tensorize
                if isinstance(formatter, TensorFormatter)
                else None  # cast in case features is None
            )
            for key, example in self.ex_iterable:
                # don't apply feature types if already applied by ex_iterable (e.g. in case of chained with_format)
                if self.features and not self.ex_iterable.is_typed:
                    example = _apply_feature_types_on_example(
                        example, self.features, token_per_repo_id=self.token_per_repo_id
                    )
                if format_dict:
                    example = format_dict(example)
                yield key, example

    def _iter_arrow(self) -> Iterator[tuple[Key, pa.Table]]:
        if not self.features:
            yield from self.ex_iterable._iter_arrow()
        for key, pa_table in self.ex_iterable._iter_arrow():
            columns = set(pa_table.column_names)
            schema = self.features.arrow_schema
            # add missing columns
            for column_name in self.features:
                if column_name not in columns:
                    col = pa.NullArray.from_buffers(pa.null(), len(pa_table), [None])
                    pa_table = pa_table.append_column(column_name, col)
            if pa_table.schema != schema:
                pa_table = cast_table_to_features(pa_table, self.features)
            yield key, pa_table

    def shuffle_data_sources(self, generator: np.random.Generator) -> "FormattedExamplesIterable":
        """Shuffle the wrapped examples iterable."""
        return FormattedExamplesIterable(
            self.ex_iterable.shuffle_data_sources(generator),
            features=self.features,
            token_per_repo_id=self.token_per_repo_id,
            formatting=self.formatting,
        )

    def shard_data_sources(self, num_shards: int, index: int, contiguous=True) -> "FormattedExamplesIterable":
        """Keep only the requested shard."""
        return FormattedExamplesIterable(
            self.ex_iterable.shard_data_sources(num_shards, index, contiguous=contiguous),
            features=self.features,
            token_per_repo_id=self.token_per_repo_id,
            formatting=self.formatting,
        )

    @property
    def num_shards(self) -> int:
        return self.ex_iterable.num_shards


@dataclass
class ShufflingConfig:
    generator: np.random.Generator
    _original_seed: Optional[int] = None


@dataclass
class DistributedConfig:
    rank: int
    world_size: int


def _maybe_add_torch_iterable_dataset_parent_class(cls):
    """Add torch.utils.data.IterableDataset as a parent class if 'torch' is available"""
    if config.TORCH_AVAILABLE:
        import torch.utils.data

        if torch.utils.data.IterableDataset not in cls.__bases__:
            cls.__bases__ += (torch.utils.data.IterableDataset,)


def _maybe_share_with_torch_persistent_workers(value: Union[int, "torch.Tensor"]) -> Union[int, "torch.Tensor"]:
    if config.TORCH_AVAILABLE:
        import torch

        if isinstance(value, torch.Tensor):
            return value.share_memory_()
        else:
            return torch.tensor(value).share_memory_()
    else:
        return value


class IterableDataset(DatasetInfoMixin):
    """A Dataset backed by an iterable."""

    def __init__(
        self,
        ex_iterable: _BaseExamplesIterable,
        info: Optional[DatasetInfo] = None,
        split: Optional[NamedSplit] = None,
        formatting: Optional[FormattingConfig] = None,
        shuffling: Optional[ShufflingConfig] = None,
        distributed: Optional[DistributedConfig] = None,
        token_per_repo_id: Optional[dict[str, Union[str, bool, None]]] = None,
    ):
        if distributed and distributed.world_size > 1 and shuffling and shuffling._original_seed is None:
            raise RuntimeError(
                "The dataset doesn't have a fixed random seed across nodes to shuffle and split the list of dataset shards by node. "
                "Please pass e.g. `seed=42` in `.shuffle()` to make all the nodes use the same seed. "
            )

        info = info.copy() if info is not None else DatasetInfo()
        DatasetInfoMixin.__init__(self, info=info, split=split)

        self._ex_iterable = copy.copy(ex_iterable)
        self._formatting = formatting
        self._shuffling = shuffling
        self._distributed = distributed
        self._token_per_repo_id: dict[str, Union[str, bool, None]] = token_per_repo_id or {}
        self._epoch: Union[int, "torch.Tensor"] = _maybe_share_with_torch_persistent_workers(0)
        self._starting_state_dict: Optional[dict] = None
        self._prepare_ex_iterable_for_iteration()  # set state_dict
        _maybe_add_torch_iterable_dataset_parent_class(self.__class__)  # subclass of torch IterableDataset

    def state_dict(self) -> dict:
        """Get the current state_dict of the dataset.
        It corresponds to the state at the latest example it yielded.

        Resuming returns exactly where the checkpoint was saved except in two cases:

        1. examples from shuffle buffers are lost when resuming and the buffers are refilled with new data
        2. combinations of `.with_format(arrow)` and batched `.map()` may skip one batch.

        Returns:
            `dict`

        Example:

        ```py
        >>> from datasets import Dataset, concatenate_datasets
        >>> ds = Dataset.from_dict({"a": range(6)}).to_iterable_dataset(num_shards=3)
        >>> for idx, example in enumerate(ds):
        ...     print(example)
        ...     if idx == 2:
        ...         state_dict = ds.state_dict()
        ...         print("checkpoint")
        ...         break
        >>> ds.load_state_dict(state_dict)
        >>> print(f"restart from checkpoint")
        >>> for example in ds:
        ...     print(example)
        ```

        which returns:
        ```
        {'a': 0}
        {'a': 1}
        {'a': 2}
        checkpoint
        restart from checkpoint
        {'a': 3}
        {'a': 4}
        {'a': 5}
        ```

        ```py
        >>> from torchdata.stateful_dataloader import StatefulDataLoader
        >>> ds = load_dataset("deepmind/code_contests", streaming=True, split="train")
        >>> dataloader = StatefulDataLoader(ds, batch_size=32, num_workers=4)
        >>> # checkpoint
        >>> state_dict = dataloader.state_dict()  # uses ds.state_dict() under the hood
        >>> # resume from checkpoint
        >>> dataloader.load_state_dict(state_dict)  # uses ds.load_state_dict() under the hood
        ```
        """
        return copy.deepcopy(self._state_dict)

    def load_state_dict(self, state_dict: dict) -> None:
        """Load the state_dict of the dataset.
        The iteration will restart at the next example from when the state was saved.

        Resuming returns exactly where the checkpoint was saved except in two cases:

        1. examples from shuffle buffers are lost when resuming and the buffers are refilled with new data
        2. combinations of `.with_format(arrow)` and batched `.map()` may skip one batch.

        Example:

        ```py
        >>> from datasets import Dataset, concatenate_datasets
        >>> ds = Dataset.from_dict({"a": range(6)}).to_iterable_dataset(num_shards=3)
        >>> for idx, example in enumerate(ds):
        ...     print(example)
        ...     if idx == 2:
        ...         state_dict = ds.state_dict()
        ...         print("checkpoint")
        ...         break
        >>> ds.load_state_dict(state_dict)
        >>> print(f"restart from checkpoint")
        >>> for example in ds:
        ...     print(example)
        ```

        which returns:
        ```
        {'a': 0}
        {'a': 1}
        {'a': 2}
        checkpoint
        restart from checkpoint
        {'a': 3}
        {'a': 4}
        {'a': 5}
        ```

        ```py
        >>> from torchdata.stateful_dataloader import StatefulDataLoader
        >>> ds = load_dataset("deepmind/code_contests", streaming=True, split="train")
        >>> dataloader = StatefulDataLoader(ds, batch_size=32, num_workers=4)
        >>> # checkpoint
        >>> state_dict = dataloader.state_dict()  # uses ds.state_dict() under the hood
        >>> # resume from checkpoint
        >>> dataloader.load_state_dict(state_dict)  # uses ds.load_state_dict() under the hood
        ```
        """
        self._starting_state_dict = state_dict

    def __repr__(self):
        return f"IterableDataset({{\n    features: {list(self._info.features.keys()) if self._info.features is not None else 'Unknown'},\n    num_shards: {self.num_shards}\n}})"

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, d):
        self.__dict__ = d
        # Re-add torch shared memory, since shared memory is not always kept when pickling
        self._epoch = _maybe_share_with_torch_persistent_workers(self._epoch)
        # Re-add torch iterable dataset as a parent class, since dynamically added parent classes are not kept when pickling
        _maybe_add_torch_iterable_dataset_parent_class(self.__class__)

    def _head(self, n=5):
        return next(iter(self.iter(batch_size=n)))

    @property
    def epoch(self) -> int:
        return int(self._epoch)

    def _effective_generator(self):
        if self._shuffling and self.epoch == 0:
            return self._shuffling.generator
        elif self._shuffling:
            # Create effective seed using self.epoch (we subtract in order to avoir overflow in long_scalars)
            effective_seed = deepcopy(self._shuffling.generator).integers(0, 1 << 63) - self.epoch
            effective_seed = (1 << 63) + effective_seed if effective_seed < 0 else effective_seed
            return np.random.default_rng(effective_seed)
        else:
            raise ValueError("This dataset is not shuffled")

    @property
    def num_shards(self) -> int:
        if self._distributed and self._ex_iterable.num_shards % self._distributed.world_size == 0:
            return self._ex_iterable.num_shards // self._distributed.world_size
        return self._ex_iterable.num_shards

    @property
    def n_shards(self) -> int:  # backward compatibility
        return self.num_shards

    def _iter_pytorch(self):
        ex_iterable = self._prepare_ex_iterable_for_iteration()
        # Fix for fsspec when using multiprocess to avoid hanging in the ML training loop. (only required for fsspec >= 0.9.0)
        # See https://github.com/fsspec/gcsfs/issues/379
        fsspec.asyn.reset_lock()
        # check if there aren't too many workers
        import torch.utils.data

        worker_info = torch.utils.data.get_worker_info()
        if self._is_main_process() and ex_iterable.num_shards < worker_info.num_workers:
            logger.warning(
                f"Too many dataloader workers: {worker_info.num_workers} (max is dataset.num_shards={ex_iterable.num_shards}). "
                f"Stopping {worker_info.num_workers - ex_iterable.num_shards} dataloader workers."
            )
            logger.info(
                f"To parallelize data loading, we give each process some shards (or data sources) to process. "
                f"Therefore it's unnecessary to have a number of workers greater than dataset.num_shards={ex_iterable.num_shards}. "
                f"To enable more parallelism, please split the dataset in more files than {ex_iterable.num_shards}."
            )
        # split workload
        _log_prefix = f"node#{self._distributed.rank} " if self._distributed else ""
        shards_indices = ex_iterable.split_shard_indices_by_worker(
            num_shards=worker_info.num_workers, index=worker_info.id, contiguous=False
        )
        if shards_indices:
            logger.debug(
                f"{_log_prefix}dataloader worker#{worker_info.id}, ': Starting to iterate over {len(shards_indices)}/{ex_iterable.num_shards} shards."
            )
            ex_iterable = ex_iterable.shard_data_sources(
                num_shards=worker_info.num_workers, index=worker_info.id, contiguous=False
            )
            self._state_dict = {
                "examples_iterable": ex_iterable._init_state_dict(),
                "epoch": self.epoch,
            }
            if self._starting_state_dict and self.epoch == self._starting_state_dict["epoch"]:
                ex_iterable.load_state_dict(self._starting_state_dict["examples_iterable"])

            if self._formatting and (ex_iterable.iter_arrow or self._formatting.is_table):
                formatter = get_formatter(self._formatting.format_type, features=self.features)
                if ex_iterable.iter_arrow:
                    iterator = ex_iterable.iter_arrow()
                else:
                    iterator = _convert_to_arrow(ex_iterable, batch_size=1)
                for key, pa_table in iterator:
                    yield formatter.format_row(pa_table)
                return
            else:
                for key, example in ex_iterable:
                    # no need to format thanks to FormattedExamplesIterable
                    yield example
            logger.debug(
                f"{_log_prefix}dataloader worker#{worker_info.id}, ': Finished iterating over {len(shards_indices)}/{ex_iterable.num_shards} shards."
            )
        else:
            logger.debug(
                f"{_log_prefix}dataloader worker#{worker_info.id}, ': Stopping... Number of dataset shards < num_workers ({ex_iterable.num_shards}<{worker_info.num_workers})."
            )

    def _is_main_process(self):
        if self._distributed and self._distributed.rank > 0:
            return False
        if "torch" in sys.modules:
            import torch.utils.data

            worker_info = torch.utils.data.get_worker_info()
            if worker_info is not None and worker_info.id > 0:
                return False
        return True

    def _prepare_ex_iterable_for_iteration(
        self, batch_size: int = 1, drop_last_batch: bool = False
    ) -> _BaseExamplesIterable:
        ex_iterable = self._ex_iterable
        if self._formatting and (ex_iterable.iter_arrow or self._formatting.is_table):
            ex_iterable = RebatchedArrowExamplesIterable(
                ex_iterable, batch_size=batch_size, drop_last_batch=drop_last_batch
            )
        if self._shuffling:
            ex_iterable = ex_iterable.shuffle_data_sources(self._effective_generator())
        else:
            ex_iterable = ex_iterable

        if self._distributed:
            rank = self._distributed.rank
            world_size = self._distributed.world_size
            if ex_iterable.num_shards % world_size == 0:
                if self._is_main_process():
                    num_shards_per_node = ex_iterable.num_shards // world_size
                    plural = "s" if num_shards_per_node > 1 else ""
                    logger.info(
                        f"Assigning {num_shards_per_node} shard{plural} (or data source{plural}) of the dataset to each node."
                    )
                ex_iterable = ex_iterable.shard_data_sources(num_shards=world_size, index=rank, contiguous=False)
            else:
                if self._is_main_process():
                    logger.info(
                        f"Assigning 1 out of {world_size} examples of the dataset to each node. The others are skipped during the iteration."
                    )
                    logger.info(
                        f"It is more optimized to distribute the dataset shards (or data sources) across nodes. "
                        f"You can do that by using a dataset with number of shards that is a factor of world_size={world_size}. "
                        f"The current dataset has {ex_iterable.num_shards} which is not a factor of {world_size}"
                    )
                ex_iterable = StepExamplesIterable(ex_iterable, step=world_size, offset=rank)

        if self._formatting or (self.features and ex_iterable.features != self.features):
            ex_iterable = FormattedExamplesIterable(
                ex_iterable,
                formatting=self._formatting,
                features=self.features,
                token_per_repo_id=self._token_per_repo_id,
            )

        self._state_dict = {
            "examples_iterable": ex_iterable._init_state_dict(),
            "epoch": self.epoch,
        }
        if self._starting_state_dict and self.epoch == self._starting_state_dict["epoch"]:
            ex_iterable.load_state_dict(self._starting_state_dict["examples_iterable"])
        return ex_iterable

    def __iter__(self):
        if "torch" in sys.modules:
            import torch.utils.data

            worker_info = torch.utils.data.get_worker_info()
            if isinstance(self, torch.utils.data.IterableDataset) and worker_info is not None:
                # We're a torch.utils.data.IterableDataset in a PyTorch worker process
                yield from self._iter_pytorch()
                return

        ex_iterable = self._prepare_ex_iterable_for_iteration()
        if self._formatting and (ex_iterable.iter_arrow or self._formatting.is_table):
            formatter = get_formatter(self._formatting.format_type, features=self.features)
            if ex_iterable.iter_arrow:
                iterator = ex_iterable.iter_arrow()
            else:
                iterator = _convert_to_arrow(ex_iterable, batch_size=1)
            for key, pa_table in iterator:
                yield formatter.format_row(pa_table)
            return

        for key, example in ex_iterable:
            # no need to format thanks to FormattedExamplesIterable
            yield example

    def iter(self, batch_size: int, drop_last_batch: bool = False):
        """Iterate through the batches of size `batch_size`.

        Args:
            batch_size (:obj:`int`): size of each batch to yield.
            drop_last_batch (:obj:`bool`, default `False`): Whether a last batch smaller than the batch_size should be
                dropped
        """

        if self._formatting:
            formatter = get_formatter(self._formatting.format_type, features=self.features)
            format_dict = formatter.recursive_tensorize if isinstance(formatter, TensorFormatter) else None
        else:
            format_dict = None

        ex_iterable = self._prepare_ex_iterable_for_iteration(batch_size=batch_size, drop_last_batch=drop_last_batch)
        if self._formatting and (ex_iterable.iter_arrow or self._formatting.is_table):
            if ex_iterable.iter_arrow:
                iterator = ex_iterable.iter_arrow()
            else:
                iterator = _convert_to_arrow(ex_iterable, batch_size=batch_size, drop_last_batch=drop_last_batch)
            for key, pa_table in iterator:
                yield formatter.format_batch(pa_table)
            return

        iterator = iter(ex_iterable)
        for key, example in iterator:
            # If batched, first build the batch
            examples = [example] + [example for key, example in islice(iterator, batch_size - 1)]
            if drop_last_batch and len(examples) < batch_size:  # ignore last batch
                return
            batch = _examples_to_batch(examples)
            # we need to format here in case we need to stack tensors together
            yield format_dict(batch) if format_dict else batch

    @staticmethod
    def from_generator(
        generator: Callable,
        features: Optional[Features] = None,
        gen_kwargs: Optional[dict] = None,
        split: NamedSplit = Split.TRAIN,
    ) -> "IterableDataset":
        """Create an Iterable Dataset from a generator.

        Args:
            generator (`Callable`):
                A generator function that `yields` examples.
            features (`Features`, *optional*):
                Dataset features.
            gen_kwargs(`dict`, *optional*):
                Keyword arguments to be passed to the `generator` callable.
                You can define a sharded iterable dataset by passing the list of shards in `gen_kwargs`.
                This can be used to improve shuffling and when iterating over the dataset with multiple workers.
            split ([`NamedSplit`], defaults to `Split.TRAIN`):
                Split name to be assigned to the dataset.

                <Added version="2.21.0"/>
        Returns:
            `IterableDataset`

        Example:

        ```py
        >>> def gen():
        ...     yield {"text": "Good", "label": 0}
        ...     yield {"text": "Bad", "label": 1}
        ...
        >>> ds = IterableDataset.from_generator(gen)
        ```

        ```py
        >>> def gen(shards):
        ...     for shard in shards:
        ...         with open(shard) as f:
        ...             for line in f:
        ...                 yield {"line": line}
        ...
        >>> shards = [f"data{i}.txt" for i in range(32)]
        >>> ds = IterableDataset.from_generator(gen, gen_kwargs={"shards": shards})
        >>> ds = ds.shuffle(seed=42, buffer_size=10_000)  # shuffles the shards order + uses a shuffle buffer
        >>> from torch.utils.data import DataLoader
        >>> dataloader = DataLoader(ds.with_format("torch"), num_workers=4)  # give each worker a subset of 32/4=8 shards
        ```
        """
        from .io.generator import GeneratorDatasetInputStream

        return GeneratorDatasetInputStream(
            generator=generator, features=features, gen_kwargs=gen_kwargs, streaming=True, split=split
        ).read()

    @staticmethod
    def from_spark(
        df: "pyspark.sql.DataFrame",
        split: Optional[NamedSplit] = None,
        features: Optional[Features] = None,
        **kwargs,
    ) -> "IterableDataset":
        """Create an IterableDataset from Spark DataFrame. The dataset is streamed to the driver in batches.

        Args:
            df (`pyspark.sql.DataFrame`):
                The DataFrame containing the desired data.
            split (`NamedSplit`, *optional*):
                Split name to be assigned to the dataset.
            features (`Features`, *optional*):
                Dataset features.

        Returns:
            [`IterableDataset`]

        Example:

        ```py
        >>> df = spark.createDataFrame(
        >>>     data=[[1, "Elia"], [2, "Teo"], [3, "Fang"]],
        >>>     columns=["id", "name"],
        >>> )
        >>> ds = IterableDataset.from_spark(df)
        ```
        """
        from .io.spark import SparkDatasetReader

        if sys.platform == "win32":
            raise OSError("IterableDataset.from_spark is not currently supported on Windows")

        return SparkDatasetReader(
            df,
            split=split,
            features=features,
            streaming=True,
            **kwargs,
        ).read()

    @staticmethod
    def from_file(filename: str) -> "IterableDataset":
        """Instantiate a IterableDataset from Arrow table at filename.

        Args:
            filename (`str`):
                File name of the dataset.

        Returns:
            [`IterableDataset`]
        """
        pa_table_schema = read_schema_from_file(filename)
        inferred_features = Features.from_arrow_schema(pa_table_schema)
        ex_iterable = ArrowExamplesIterable(Dataset._generate_tables_from_cache_file, kwargs={"filename": filename})
        return IterableDataset(ex_iterable=ex_iterable, info=DatasetInfo(features=inferred_features))

    def with_format(
        self,
        type: Optional[str] = None,
    ) -> "IterableDataset":
        """
        Return a dataset with the specified format.

        Args:

            type (`str`, *optional*):
                Either output type selected in `[None, 'numpy', 'torch', 'tensorflow', 'jax', 'arrow', 'pandas', 'polars']`.
                `None` means it returns python objects (default).

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> from transformers import AutoTokenizer
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="validation", streaming=True)
        >>> tokenizer = AutoTokenizer.from_pretrained("bert-base-cased")
        >>> ds = ds.map(lambda x: tokenizer(x['text'], truncation=True, padding=True), batched=True)
        >>> ds = ds.with_format("torch")
        >>> next(iter(ds))
        {'text': 'compassionately explores the seemingly irreconcilable situation between conservative christian parents and their estranged gay and lesbian children .',
         'label': tensor(1),
         'input_ids': tensor([  101, 18027, 16310, 16001,  1103,  9321,   178, 11604,  7235,  6617,
                1742,  2165,  2820,  1206,  6588, 22572, 12937,  1811,  2153,  1105,
                1147, 12890, 19587,  6463,  1105, 15026,  1482,   119,   102,     0,
                    0,     0,     0,     0,     0,     0,     0,     0,     0,     0,
                    0,     0,     0,     0,     0,     0,     0,     0,     0,     0,
                    0,     0,     0,     0,     0,     0,     0,     0,     0,     0,
                    0,     0,     0,     0,     0,     0,     0,     0,     0,     0,
                    0,     0,     0,     0,     0,     0,     0,     0,     0,     0,
                    0,     0,     0,     0]),
         'token_type_ids': tensor([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
         'attention_mask': tensor([1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
                1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])}
        ```
        """
        type = get_format_type_from_alias(type)
        # TODO(QL): add format_kwargs
        # TODO(QL): add format_columns and return_all_columns
        # TODO(QL): add pandas format
        return IterableDataset(
            ex_iterable=self._ex_iterable,
            info=self._info.copy(),
            split=self._split,
            formatting=FormattingConfig(format_type=type),
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def map(
        self,
        function: Optional[Callable] = None,
        with_indices: bool = False,
        input_columns: Optional[Union[str, list[str]]] = None,
        batched: bool = False,
        batch_size: Optional[int] = 1000,
        drop_last_batch: bool = False,
        remove_columns: Optional[Union[str, list[str]]] = None,
        features: Optional[Features] = None,
        fn_kwargs: Optional[dict] = None,
    ) -> "IterableDataset":
        """
        Apply a function to all the examples in the iterable dataset (individually or in batches) and update them.
        If your function returns a column that already exists, then it overwrites it.
        The function is applied on-the-fly on the examples when iterating over the dataset.

        You can specify whether the function should be batched or not with the `batched` parameter:

        - If batched is `False`, then the function takes 1 example in and should return 1 example.
          An example is a dictionary, e.g. `{"text": "Hello there !"}`.
        - If batched is `True` and `batch_size` is 1, then the function takes a batch of 1 example as input and can return a batch with 1 or more examples.
          A batch is a dictionary, e.g. a batch of 1 example is {"text": ["Hello there !"]}.
        - If batched is `True` and `batch_size` is `n` > 1, then the function takes a batch of `n` examples as input and can return a batch with `n` examples, or with an arbitrary number of examples.
          Note that the last batch may have less than `n` examples.
          A batch is a dictionary, e.g. a batch of `n` examples is `{"text": ["Hello there !"] * n}`.

        If the function is asynchronous, then `map` will run your function in parallel, with up to one thousand simulatenous calls.
        It is recommended to use a `asyncio.Semaphore` in your function if you want to set a maximum number of operations that can run at the same time.

        Args:
            function (`Callable`, *optional*, defaults to `None`):
                Function applied on-the-fly on the examples when you iterate on the dataset.
                It must have one of the following signatures:

                - `function(example: Dict[str, Any]) -> Dict[str, Any]` if `batched=False` and `with_indices=False`
                - `function(example: Dict[str, Any], idx: int) -> Dict[str, Any]` if `batched=False` and `with_indices=True`
                - `function(batch: Dict[str, List]) -> Dict[str, List]` if `batched=True` and `with_indices=False`
                - `function(batch: Dict[str, List], indices: List[int]) -> Dict[str, List]` if `batched=True` and `with_indices=True`

                For advanced usage, the function can also return a `pyarrow.Table`.
                If the function is asynchronous, then `map` will run your function in parallel.
                Moreover if your function returns nothing (`None`), then `map` will run your function and return the dataset unchanged.
                If no function is provided, default to identity function: `lambda x: x`.
            with_indices (`bool`, defaults to `False`):
                Provide example indices to `function`. Note that in this case the signature of `function` should be `def function(example, idx[, rank]): ...`.
            input_columns (`Optional[Union[str, List[str]]]`, defaults to `None`):
                The columns to be passed into `function`
                as positional arguments. If `None`, a dict mapping to all formatted columns is passed as one argument.
            batched (`bool`, defaults to `False`):
                Provide batch of examples to `function`.
            batch_size (`int`, *optional*, defaults to `1000`):
                Number of examples per batch provided to `function` if `batched=True`.
                `batch_size <= 0` or `batch_size == None` then provide the full dataset as a single batch to `function`.
            drop_last_batch (`bool`, defaults to `False`):
                Whether a last batch smaller than the batch_size should be
                dropped instead of being processed by the function.
            remove_columns (`[List[str]]`, *optional*, defaults to `None`):
                Remove a selection of columns while doing the mapping.
                Columns will be removed before updating the examples with the output of `function`, i.e. if `function` is adding
                columns with names in `remove_columns`, these columns will be kept.
            features (`[Features]`, *optional*, defaults to `None`):
                Feature types of the resulting dataset.
            fn_kwargs (`Dict`, *optional*, default `None`):
                Keyword arguments to be passed to `function`.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> def add_prefix(example):
        ...     example["text"] = "Review: " + example["text"]
        ...     return example
        >>> ds = ds.map(add_prefix)
        >>> list(ds.take(3))
        [{'label': 1,
         'text': 'Review: the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'Review: the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'Review: effective but too-tepid biopic'}]
        ```
        """
        if isinstance(input_columns, str):
            input_columns = [input_columns]
        if isinstance(remove_columns, str):
            remove_columns = [remove_columns]
        if function is None:
            function = identity_func
        if fn_kwargs is None:
            fn_kwargs = {}

        ex_iterable = self._ex_iterable
        # no need to apply features if ex_iterable is typed and if there was no cast_column()
        input_features = (
            None
            if (ex_iterable.is_typed and (self._info.features is None or self._info.features == ex_iterable.features))
            else self._info.features
        )

        if self._formatting and self._formatting.is_table:
            # apply formatting before iter_arrow to keep map examples iterable happy
            ex_iterable = FormattedExamplesIterable(
                ex_iterable,
                formatting=copy.deepcopy(self._formatting),
                features=input_features,
                token_per_repo_id=self._token_per_repo_id,
            )
            ex_iterable = RebatchedArrowExamplesIterable(
                ex_iterable, batch_size=batch_size if batched else 1, drop_last_batch=drop_last_batch
            )
        else:
            if self._formatting and self._ex_iterable.iter_arrow:
                ex_iterable = RebatchedArrowExamplesIterable(
                    self._ex_iterable, batch_size=batch_size if batched else 1, drop_last_batch=drop_last_batch
                )
            if self._formatting or input_features:
                # apply formatting after iter_arrow to avoid re-encoding the examples
                ex_iterable = FormattedExamplesIterable(
                    ex_iterable,
                    formatting=copy.deepcopy(self._formatting),
                    features=input_features,
                    token_per_repo_id=self._token_per_repo_id,
                )

        ex_iterable = MappedExamplesIterable(
            ex_iterable,
            function=function,
            with_indices=with_indices,
            input_columns=input_columns,
            batched=batched,
            batch_size=batch_size,
            drop_last_batch=drop_last_batch,
            remove_columns=remove_columns,
            fn_kwargs=fn_kwargs,
            formatting=self._formatting,
            features=features,
        )
        info = self.info.copy()
        info.features = features
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def filter(
        self,
        function: Optional[Callable] = None,
        with_indices=False,
        input_columns: Optional[Union[str, list[str]]] = None,
        batched: bool = False,
        batch_size: Optional[int] = 1000,
        fn_kwargs: Optional[dict] = None,
    ) -> "IterableDataset":
        """Apply a filter function to all the elements so that the dataset only includes examples according to the filter function.
        The filtering is done on-the-fly when iterating over the dataset.

        If the function is asynchronous, then `filter` will run your function in parallel, with up to one thousand simulatenous calls (configurable).
        It is recommended to use a `asyncio.Semaphore` in your function if you want to set a maximum number of operations that can run at the same time.

        Args:
            function (`Callable`):
                Callable with one of the following signatures:

                - `function(example: Dict[str, Any]) -> bool` if `with_indices=False, batched=False`
                - `function(example: Dict[str, Any], indices: int) -> bool` if `with_indices=True, batched=False`
                - `function(example: Dict[str, List]) -> List[bool]` if `with_indices=False, batched=True`
                - `function(example: Dict[str, List], indices: List[int]) -> List[bool]` if `with_indices=True, batched=True`

                If the function is asynchronous, then `filter` will run your function in parallel.
                If no function is provided, defaults to an always True function: `lambda x: True`.
            with_indices (`bool`, defaults to `False`):
                Provide example indices to `function`. Note that in this case the signature of `function` should be `def function(example, idx): ...`.
            input_columns (`str` or `List[str]`, *optional*):
                The columns to be passed into `function` as
                positional arguments. If `None`, a dict mapping to all formatted columns is passed as one argument.
            batched (`bool`, defaults to `False`):
                Provide batch of examples to `function`.
            batch_size (`int`, *optional*, default `1000`):
                Number of examples per batch provided to `function` if `batched=True`.
            fn_kwargs (`Dict`, *optional*, default `None`):
                Keyword arguments to be passed to `function`.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> ds = ds.filter(lambda x: x["label"] == 0)
        >>> list(ds.take(3))
        [{'label': 0, 'movie_review': 'simplistic , silly and tedious .'},
         {'label': 0,
         'movie_review': "it's so laddish and juvenile , only teenage boys could possibly find it funny ."},
         {'label': 0,
         'movie_review': 'exploitative and largely devoid of the depth or sophistication that would make watching such a graphic treatment of the crimes bearable .'}]
        ```
        """
        if isinstance(input_columns, str):
            input_columns = [input_columns]

        # We need the examples to be decoded for certain feature types like Image or Audio,
        # format and type before filtering
        ex_iterable = self._ex_iterable
        if self._info.features or self._formatting:
            ex_iterable = FormattedExamplesIterable(
                ex_iterable,
                formatting=self._formatting,
                features=None if ex_iterable.is_typed else self._info.features,
                token_per_repo_id=self._token_per_repo_id,
            )

        ex_iterable = FilteredExamplesIterable(
            ex_iterable,
            function=function,
            with_indices=with_indices,
            input_columns=input_columns,
            batched=batched,
            batch_size=batch_size,
            fn_kwargs=fn_kwargs,
            formatting=self._formatting,
        )
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=self._info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def shuffle(
        self, seed=None, generator: Optional[np.random.Generator] = None, buffer_size: int = 1000
    ) -> "IterableDataset":
        """
        Randomly shuffles the elements of this dataset.

        This dataset fills a buffer with `buffer_size` elements, then randomly samples elements from this buffer,
        replacing the selected elements with new elements. For perfect shuffling, a buffer size greater than or
        equal to the full size of the dataset is required.

        For instance, if your dataset contains 10,000 elements but `buffer_size` is set to 1000, then `shuffle` will
        initially select a random element from only the first 1000 elements in the buffer. Once an element is
        selected, its space in the buffer is replaced by the next (i.e. 1,001-st) element,
        maintaining the 1000 element buffer.

        If the dataset is made of several shards, it also does shuffle the order of the shards.
        However if the order has been fixed by using [`~datasets.IterableDataset.skip`] or [`~datasets.IterableDataset.take`]
        then the order of the shards is kept unchanged.

        Args:
            seed (`int`, *optional*, defaults to `None`):
                Random seed that will be used to shuffle the dataset.
                It is used to sample from the shuffle buffer and also to shuffle the data shards.
            generator (`numpy.random.Generator`, *optional*):
                Numpy random Generator to use to compute the permutation of the dataset rows.
                If `generator=None` (default), uses `np.random.default_rng` (the default BitGenerator (PCG64) of NumPy).
            buffer_size (`int`, defaults to `1000`):
                Size of the buffer.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> list(ds.take(3))
        [{'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'effective but too-tepid biopic'}]
        >>> shuffled_ds = ds.shuffle(seed=42)
        >>> list(shuffled_ds.take(3))
        [{'label': 1,
         'text': "a sports movie with action that's exciting on the field and a story you care about off it ."},
         {'label': 1,
         'text': 'at its best , the good girl is a refreshingly adult take on adultery . . .'},
         {'label': 1,
         'text': "sam jones became a very lucky filmmaker the day wilco got dropped from their record label , proving that one man's ruin may be another's fortune ."}]
        ```
        """
        if generator is None:
            generator = np.random.default_rng(seed)
        else:
            generator = deepcopy(generator)
        shuffling = ShufflingConfig(generator=generator, _original_seed=seed)
        return IterableDataset(
            ex_iterable=BufferShuffledExamplesIterable(
                self._ex_iterable, buffer_size=buffer_size, generator=generator
            ),
            info=self._info.copy(),
            split=self._split,
            formatting=self._formatting,
            shuffling=shuffling,
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def set_epoch(self, epoch: int):
        self._epoch += epoch - self._epoch  # update torch value in shared memory in-place

    def skip(self, n: int) -> "IterableDataset":
        """
        Create a new [`IterableDataset`] that skips the first `n` elements.

        Args:
            n (`int`):
                Number of elements to skip.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> list(ds.take(3))
        [{'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'effective but too-tepid biopic'}]
        >>> ds = ds.skip(1)
        >>> list(ds.take(3))
        [{'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'effective but too-tepid biopic'},
         {'label': 1,
         'text': 'if you sometimes like to go to the movies to have fun , wasabi is a good place to start .'}]
        ```
        """
        ex_iterable = SkipExamplesIterable(
            self._ex_iterable,
            n,
            block_sources_order_when_shuffling=self._shuffling is None,
            split_when_sharding=self._distributed is None,
        )
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=self._info.copy(),
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def repeat(self, num_times: Optional[int]) -> "IterableDataset":
        """
        Create a new [`IterableDataset`] that repeats the underlying dataset `num_times` times.

        N.B. The effect of calling shuffle after repeat depends significantly on buffer size.
        With buffer_size 1, duplicate data is never seen in the same iteration, even after shuffling:
        ds.repeat(n).shuffle(seed=42, buffer_size=1) is equivalent to ds.shuffle(seed=42, buffer_size=1).repeat(n),
        and only shuffles shard orders within each iteration.
        With buffer size >= (num samples in the dataset * num_times), we get full shuffling of the repeated data, i.e. we can observe duplicates in
        the same iteration.

        Args:
            num_times (`int`) or (`None`):
                Number of times to repeat the dataset. If `None`, the dataset will be repeated indefinitely.

        Example:
        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train")
        >>> ds = ds.take(2).repeat(2)
        >>> list(ds)
        [{'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'effective but too-tepid biopic'},
         {'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'},
         {'label': 1, 'text': 'effective but too-tepid biopic'}]
        ```
        """
        return IterableDataset(
            ex_iterable=RepeatExamplesIterable(self._ex_iterable, num_times=num_times),
            info=self._info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def take(self, n: int) -> "IterableDataset":
        """
        Create a new [`IterableDataset`] with only the first `n` elements.

        Args:
            n (`int`):
                Number of elements to take.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> small_ds = ds.take(2)
        >>> list(small_ds)
        [{'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'},
         {'label': 1,
         'text': 'the gorgeously elaborate continuation of " the lord of the rings " trilogy is so huge that a column of words cannot adequately describe co-writer/director peter jackson\'s expanded vision of j . r . r . tolkien\'s middle-earth .'}]
        ```
        """
        ex_iterable = TakeExamplesIterable(
            self._ex_iterable,
            n,
            block_sources_order_when_shuffling=self._shuffling is None,
            split_when_sharding=self._distributed is None,
        )
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=self._info.copy(),
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def shard(
        self,
        num_shards: int,
        index: int,
        contiguous: bool = True,
    ) -> "IterableDataset":
        """Return the `index`-nth shard from dataset split into `num_shards` pieces.

        This shards deterministically. `dataset.shard(n, i)` splits the dataset into contiguous chunks,
        so it can be easily concatenated back together after processing. If `dataset.num_shards % n == l`, then the
        first `l` datasets each have `(dataset.num_shards // n) + 1` shards, and the remaining datasets have `(dataset.num_shards // n)` shards.
        `datasets.concatenate_datasets([dset.shard(n, i) for i in range(n)])` returns a dataset with the same order as the original.
        In particular, `dataset.shard(dataset.num_shards, i)` returns a dataset with 1 shard.

        Note: n should be less or equal to the number of shards in the dataset `dataset.num_shards`.

        On the other hand, `dataset.shard(n, i, contiguous=False)` contains all the shards of the dataset whose index mod `n = i`.

        Be sure to shard before using any randomizing operator (such as `shuffle`).
        It is best if the shard operator is used early in the dataset pipeline.

        Args:
            num_shards (`int`):
                How many shards to split the dataset into.
            index (`int`):
                Which shard to select and return.
            contiguous: (`bool`, defaults to `True`):
                Whether to select contiguous blocks of indices for shards.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("amazon_polarity", split="train", streaming=True)
        >>> ds
        Dataset({
            features: ['label', 'title', 'content'],
            num_shards: 4
        })
        >>> ds.shard(num_shards=2, index=0)
        Dataset({
            features: ['label', 'title', 'content'],
            num_shards: 2
        })
        ```
        """
        ex_iterable = self._ex_iterable.shard_data_sources(num_shards=num_shards, index=index, contiguous=contiguous)
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=self._info.copy(),
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    @property
    def column_names(self) -> Optional[list[str]]:
        """Names of the columns in the dataset.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="validation", streaming=True)
        >>> ds.column_names
        ['text', 'label']
        ```
        """
        return list(self._info.features.keys()) if self._info.features is not None else None

    def add_column(self, name: str, column: Union[list, np.array]) -> "IterableDataset":
        """Add column to Dataset.

        Args:
            name (str): Column name.
            column (list or np.array): Column data to be added.

        Returns:
            `IterableDataset`
        """
        return self.map(partial(add_column_fn, name=name, column=column), with_indices=True)

    def rename_column(self, original_column_name: str, new_column_name: str) -> "IterableDataset":
        """
        Rename a column in the dataset, and move the features associated to the original column under the new column
        name.

        Args:
            original_column_name (`str`):
                Name of the column to rename.
            new_column_name (`str`):
                New name for the column.

        Returns:
            `IterableDataset`: A copy of the dataset with a renamed column.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> next(iter(ds))
        {'label': 1,
         'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'}
        >>> ds = ds.rename_column("text", "movie_review")
        >>> next(iter(ds))
        {'label': 1,
         'movie_review': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'}
        ```
        """
        return self.rename_columns({original_column_name: new_column_name})

    def rename_columns(self, column_mapping: dict[str, str]) -> "IterableDataset":
        """
        Rename several columns in the dataset, and move the features associated to the original columns under
        the new column names.

        Args:
            column_mapping (`Dict[str, str]`): A mapping of columns to rename to their new names

        Returns:
            `IterableDataset`: A copy of the dataset with renamed columns
        """

        original_features = self._info.features.copy() if self._info.features else None
        ds_iterable = self.map(
            partial(_rename_columns_fn, column_mapping=column_mapping), remove_columns=list(column_mapping)
        )
        if original_features is not None:
            ds_iterable._info.features = Features(
                {
                    column_mapping[col] if col in column_mapping.keys() else col: feature
                    for col, feature in original_features.items()
                }
            )
        return ds_iterable

    def remove_columns(self, column_names: Union[str, list[str]]) -> "IterableDataset":
        """
        Remove one or several column(s) in the dataset and the features associated to them.
        The removal is done on-the-fly on the examples when iterating over the dataset.


        Args:
            column_names (`Union[str, List[str]]`):
                Name of the column(s) to remove.

        Returns:
            `IterableDataset`: A copy of the dataset object without the columns to remove.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> next(iter(ds))
        {'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .', 'label': 1}
        >>> ds = ds.remove_columns("label")
        >>> next(iter(ds))
        {'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'}
        ```
        """
        original_features = self._info.features.copy() if self._info.features else None
        ds_iterable = self.map(remove_columns=column_names)
        if original_features is not None:
            ds_iterable._info.features = original_features.copy()
            for col, _ in original_features.items():
                if col in column_names:
                    del ds_iterable._info.features[col]

        return ds_iterable

    def select_columns(self, column_names: Union[str, list[str]]) -> "IterableDataset":
        """Select one or several column(s) in the dataset and the features
        associated to them. The selection is done on-the-fly on the examples
        when iterating over the dataset.


        Args:
            column_names (`Union[str, List[str]]`):
                Name of the column(s) to select.

        Returns:
            `IterableDataset`: A copy of the dataset object with selected columns.

        Example:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> next(iter(ds))
        {'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .', 'label': 1}
        >>> ds = ds.select_columns("text")
        >>> next(iter(ds))
        {'text': 'the rock is destined to be the 21st century\'s new " conan " and that he\'s going to make a splash even greater than arnold schwarzenegger , jean-claud van damme or steven segal .'}
        ```
        """
        if isinstance(column_names, str):
            column_names = [column_names]

        if self._info:
            info = copy.deepcopy(self._info)
            if self._info.features is not None:
                missing_columns = set(column_names) - set(self._info.features.keys())
                if missing_columns:
                    raise ValueError(
                        f"Column name {list(missing_columns)} not in the "
                        "dataset. Columns in the dataset: "
                        f"{list(self._info.features.keys())}."
                    )
                info.features = Features({c: info.features[c] for c in column_names})

        ex_iterable = SelectColumnsIterable(self._ex_iterable, column_names)
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=info,
            split=self._split,
            formatting=self._formatting,
            shuffling=self._shuffling,
            distributed=self._distributed,
            token_per_repo_id=self._token_per_repo_id,
        )

    def cast_column(self, column: str, feature: FeatureType) -> "IterableDataset":
        """Cast column to feature for decoding.

        Args:
            column (`str`):
                Column name.
            feature (`Feature`):
                Target feature.

        Returns:
            `IterableDataset`

        Example:

        ```py
        >>> from datasets import load_dataset, Audio
        >>> ds = load_dataset("PolyAI/minds14", name="en-US", split="train", streaming=True)
        >>> ds.features
        {'audio': Audio(sampling_rate=8000, mono=True, decode=True, id=None),
         'english_transcription': Value(dtype='string', id=None),
         'intent_class': ClassLabel(num_classes=14, names=['abroad', 'address', 'app_error', 'atm_limit', 'balance', 'business_loan',  'card_issues', 'cash_deposit', 'direct_debit', 'freeze', 'high_value_payment', 'joint_account', 'latest_transactions', 'pay_bill'], id=None),
         'lang_id': ClassLabel(num_classes=14, names=['cs-CZ', 'de-DE', 'en-AU', 'en-GB', 'en-US', 'es-ES', 'fr-FR', 'it-IT', 'ko-KR',  'nl-NL', 'pl-PL', 'pt-PT', 'ru-RU', 'zh-CN'], id=None),
         'path': Value(dtype='string', id=None),
         'transcription': Value(dtype='string', id=None)}
        >>> ds = ds.cast_column("audio", Audio(sampling_rate=16000))
        >>> ds.features
        {'audio': Audio(sampling_rate=16000, mono=True, decode=True, id=None),
         'english_transcription': Value(dtype='string', id=None),
         'intent_class': ClassLabel(num_classes=14, names=['abroad', 'address', 'app_error', 'atm_limit', 'balance', 'business_loan',  'card_issues', 'cash_deposit', 'direct_debit', 'freeze', 'high_value_payment', 'joint_account', 'latest_transactions', 'pay_bill'], id=None),
         'lang_id': ClassLabel(num_classes=14, names=['cs-CZ', 'de-DE', 'en-AU', 'en-GB', 'en-US', 'es-ES', 'fr-FR', 'it-IT', 'ko-KR',  'nl-NL', 'pl-PL', 'pt-PT', 'ru-RU', 'zh-CN'], id=None),
         'path': Value(dtype='string', id=None),
         'transcription': Value(dtype='string', id=None)}
        ```
        """
        info = self._info.copy()
        info.features[column] = feature
        return IterableDataset(
            ex_iterable=self._ex_iterable,
            info=info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def cast(
        self,
        features: Features,
    ) -> "IterableDataset":
        """
        Cast the dataset to a new set of features.

        Args:
            features ([`Features`]):
                New features to cast the dataset to.
                The name of the fields in the features must match the current column names.
                The type of the data must also be convertible from one type to the other.
                For non-trivial conversion, e.g. `string` <-> `ClassLabel` you should use [`~Dataset.map`] to update the Dataset.

        Returns:
            `IterableDataset`: A copy of the dataset with casted features.

        Example:

        ```py
        >>> from datasets import load_dataset, ClassLabel, Value
        >>> ds = load_dataset("cornell-movie-review-data/rotten_tomatoes", split="train", streaming=True)
        >>> ds.features
        {'label': ClassLabel(names=['neg', 'pos'], id=None),
         'text': Value(dtype='string', id=None)}
        >>> new_features = ds.features.copy()
        >>> new_features["label"] = ClassLabel(names=["bad", "good"])
        >>> new_features["text"] = Value("large_string")
        >>> ds = ds.cast(new_features)
        >>> ds.features
        {'label': ClassLabel(names=['bad', 'good'], id=None),
         'text': Value(dtype='large_string', id=None)}
        ```
        """
        info = self._info.copy()
        info.features = features
        return IterableDataset(
            ex_iterable=self._ex_iterable,
            info=info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def decode(self, enable: bool = True, num_threads: int = 0) -> "IterableDataset":
        """
        Enable or disable the dataset features decoding for audio, image, video.

        When enabled (default), media types are decoded:

        * audio -> dict of "array" and "sampling_rate" and "path"
        * image -> PIL.Image
        * video -> torchvision.io.VideoReader

        You can enable multithreading using `num_threads`. This is especially useful to speed up remote
        data streaming. However it can be slower than `num_threads=0` for local data on fast disks.

        Disabling decoding is useful if you want to iterate on the paths or bytes of the media files
        without actually decoding their content. To disable decoding you can use `.decode(False)`, which
        is equivalent to calling `.cast()` or `.cast_column()` with all the Audio, Image and Video types
        set to `decode=False`.

        Args:
            enable (`bool`, defaults to `True`):
                Enable or disable features decoding.
            num_threads (`int`, defaults to `0`):
                Enable multithreading for features decoding.

        Returns:
            `IterableDataset`: A copy of the dataset with casted features.

        Examples:

        Disable decoding:

        ```py
        >>> from datasets import load_dataset
        >>> ds = load_dataset("sshh12/planet-textures", split="train", streaming=True)
        >>> next(iter(ds))
        {'image': <PIL.PngImagePlugin.PngImageFile image mode=RGB size=2048x1024>,
        'text': 'A distant celestial object with an icy crust, displaying a light blue shade, covered with round pits and rugged terrains.'}
        >>> ds = ds.decode(False)
        >>> ds.features
        {'image': Image(mode=None, decode=False, id=None),
        'text': Value(dtype='string', id=None)}
        >>> next(iter(ds))
        {
          'image': {
            'path': 'hf://datasets/sshh12/planet-textures@69dc4cef7a5c4b2cfe387727ec8ea73d4bff7302/train/textures/0000.png',
            'bytes': None
          },
          'text': 'A distant celestial object with an icy crust, displaying a light blue shade, covered with round pits and rugged terrains.'
        }
        ```

        Speed up streaming with multithreading:

        ```py
        >>> import os
        >>> from datasets import load_dataset
        >>> from tqdm import tqdm
        >>> ds = load_dataset("sshh12/planet-textures", split="train", streaming=True)
        >>> num_threads = min(32, (os.cpu_count() or 1) + 4)
        >>> ds = ds.decode(num_threads=num_threads)
        >>> for _ in tqdm(ds):  # 20 times faster !
        ...     ...
        ```
        """
        if not self.features:
            raise ValueError(
                "Features decoding is only available for datasets with known features, but features are Unknown. "
                "Please set the datasets features with `ds = ds.cast(features)`."
            )
        ds = self

        def set_decoding(decode: bool, feature):
            if hasattr(feature, "decode"):
                feature.decode = decode

        if enable and num_threads > 0:
            disabled_decoding_features = self.features.copy()
            enabled_decoding_features = self.features.copy()

            _visit(disabled_decoding_features, partial(set_decoding, False))
            _visit(enabled_decoding_features, partial(set_decoding, True))
            ds = ds.cast(disabled_decoding_features)
            pool = multiprocessing.pool.ThreadPool(num_threads)
            func = partial(_apply_async, pool, enabled_decoding_features.decode_example)
            ds = ds.map(func, features=enabled_decoding_features)
            assert isinstance(ds._ex_iterable, MappedExamplesIterable)
            ds._ex_iterable.max_num_running_async_map_functions_in_parallel = 2 * num_threads
        else:
            features = ds.features.copy()
            _visit(features, partial(set_decoding, enable))
            ds = ds.cast(features)
        return ds

    def _step(self, step: int, offset: int) -> "IterableDataset":
        ex_iterable = StepExamplesIterable(self._ex_iterable, step=step, offset=offset)
        return IterableDataset(
            ex_iterable=ex_iterable,
            info=self._info.copy(),
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def _resolve_features(self):
        if self.features is not None:
            return self
        elif self._ex_iterable.is_typed:
            features = self._ex_iterable.features
        else:
            features = _infer_features_from_batch(self.with_format(None)._head())
        info = self.info.copy()
        info.features = features
        return IterableDataset(
            ex_iterable=self._ex_iterable,
            info=info,
            split=self._split,
            formatting=self._formatting,
            shuffling=copy.deepcopy(self._shuffling),
            distributed=copy.deepcopy(self._distributed),
            token_per_repo_id=self._token_per_repo_id,
        )

    def batch(self, batch_size: int, drop_last_batch: bool = False) -> "IterableDataset":
        """
        Group samples from the dataset into batches.

        Args:
            batch_size (`int`): The number of samples in each batch.
            drop_last_batch (`bool`, defaults to `False`): Whether to drop the last incomplete batch.

        Example:
        ```py
        >>> ds = load_dataset("some_dataset", streaming=True)
        >>> batched_ds = ds.batch(batch_size=32)
        ```
        """

        def batch_fn(unbatched):
            return {k: [v] for k, v in unbatched.items()}

        if self.features:
            features = Features({col: [feature] for col, feature in self.features.items()})
        else:
            features = None
        return self.map(
            batch_fn, batched=True, batch_size=batch_size, drop_last_batch=drop_last_batch, features=features
        )


def _concatenate_iterable_datasets(
    dsets: list[IterableDataset],
    info: Optional[DatasetInfo] = None,
    split: Optional[NamedSplit] = None,
    axis: int = 0,
) -> IterableDataset:
    """
    Converts a list of `IterableDataset` with the same schema into a single `IterableDataset`.
    Missing data are filled with None values.

    <Added version="2.4.0"/>

    Args:
        dsets (`List[datasets.IterableDataset]`): List of Datasets to concatenate.
        info (`DatasetInfo`, optional): Dataset information, like description, citation, etc.
        split (`NamedSplit`, optional): Name of the dataset split.
        axis (``{0, 1}``, default ``0``, meaning over rows):
            Axis to concatenate over, where ``0`` means over rows (vertically) and ``1`` means over columns
            (horizontally).

            *New in version 1.6.0*

    Example:

    ```py
    >>> ds3 = _concatenate_iterable_datasets([ds1, ds2])
    ```
    """
    dsets = [d._resolve_features() for d in dsets]

    # Perform checks (and a potentional cast if axis=0)
    if axis == 0:
        _check_if_features_can_be_aligned([dset.features for dset in dsets])
    else:
        _check_column_names([col_name for dset in dsets for col_name in dset.features])

    # TODO: improve this to account for a mix of ClassLabel and Value for example
    # right now it would keep the type of the first dataset in the list
    features = Features(
        {k: v for features in _align_features([dset.features for dset in dsets]) for k, v in features.items()}
    )

    ex_iterables = [copy.deepcopy(d._ex_iterable) for d in dsets]
    if axis == 0:
        ex_iterable = VerticallyConcatenatedMultiSourcesExamplesIterable(ex_iterables)
    else:
        ex_iterable = HorizontallyConcatenatedMultiSourcesExamplesIterable(ex_iterables)
    # Set new info - we update the features
    # setting the features also ensures to fill missing columns with None
    if info is None:
        info = DatasetInfo.from_merge([d.info for d in dsets])
    else:
        info = info.copy()
    info.features = features
    # Get all the auth tokens per repository - in case the datasets come from different private repositories
    token_per_repo_id = {repo_id: token for dataset in dsets for repo_id, token in dataset._token_per_repo_id.items()}
    # Return new daset
    return IterableDataset(ex_iterable=ex_iterable, info=info, split=split, token_per_repo_id=token_per_repo_id)


def _interleave_iterable_datasets(
    datasets: list[IterableDataset],
    probabilities: Optional[list[float]] = None,
    seed: Optional[int] = None,
    info: Optional[DatasetInfo] = None,
    split: Optional[NamedSplit] = None,
    stopping_strategy: Literal["first_exhausted", "all_exhausted"] = "first_exhausted",
) -> IterableDataset:
    """
    Interleave several iterable datasets (sources) into a single iterable dataset.
    The new iterable dataset alternates between the sources to yield examples.
    If `probabilities = None` (default) the iterable dataset will cycles through the sources in order for each next example in the iteration.
    If `probabilities` is not `None, the iterable dataset will sample a random source according to the provided probabilities for each next examples in the iteration.

    <Added version="2.4.0"/>

    Args:
        datasets (`List[IterableDataset]`): list of datasets to interleave
        probabilities (`List[float]`, optional, default None): If specified, the new iterable dataset samples
            examples from one source at a time according to these probabilities.
        seed (`int`, optional, default None): The random seed used to choose a source for each example.
        stopping_strategy (`str`, defaults to `first_exhausted`):
            Two strategies are proposed right now.
            By default, `first_exhausted` is an undersampling strategy, i.e the dataset construction is stopped as soon as one dataset has ran out of samples.
            If the strategy is `all_exhausted`,  we use an oversampling strategy, i.e the dataset construction is stopped as soon as every samples of every dataset has been added at least once.
            Note that if the strategy is `all_exhausted`, the interleaved dataset size can get enormous:
            - with no probabilities, the resulting dataset will have max_length_datasets*nb_dataset samples.
            - with given probabilities, the resulting dataset will have more samples if some datasets have really low probability of visiting.

    Output:
        `datasets.IterableDataset`
    """
    datasets = [d._resolve_features() for d in datasets]

    # Perform checks
    _check_if_features_can_be_aligned([dset.features for dset in datasets])

    # TODO: improve this to account for a mix of ClassLabel and Value for example
    # right now it would keep the type of the first dataset in the list
    features = Features(
        {k: v for features in _align_features([dset.features for dset in datasets]) for k, v in features.items()}
    )

    ex_iterables = [copy.deepcopy(d._ex_iterable) for d in datasets]

    # Use cycling or random cycling of sources
    if probabilities is None:
        ex_iterable = CyclingMultiSourcesExamplesIterable(ex_iterables, stopping_strategy=stopping_strategy)
    else:
        generator = np.random.default_rng(seed)
        ex_iterable = RandomlyCyclingMultiSourcesExamplesIterable(
            ex_iterables, generator=generator, probabilities=probabilities, stopping_strategy=stopping_strategy
        )
    # Set new info - we update the features
    # setting the features also ensures to fill missing columns with None
    if info is None:
        info = DatasetInfo.from_merge([d.info for d in datasets])
    else:
        info = info.copy()
    info.features = features
    # Get all the auth tokens per repository - in case the datasets come from different private repositories
    token_per_repo_id = {
        repo_id: token for dataset in datasets for repo_id, token in dataset._token_per_repo_id.items()
    }
    # Return new daset
    return IterableDataset(ex_iterable=ex_iterable, info=info, split=split, token_per_repo_id=token_per_repo_id)


def _split_by_node_iterable_dataset(dataset: IterableDataset, rank: int, world_size: int) -> IterableDataset:
    """
    Split an iterable dataset for the node at rank `rank` in a pool of nodes of size `world_size`.

    If the dataset has a number of shards that is a factor of `world_size` (i.e. if `dataset.num_shards % world_size == 0`),
    then the shards are evenly assigned across the nodes, which is the most optimized.
    Otherwise, each node keeps 1 example out of `world_size`, skipping the other examples.

    Args:
        dataset ([`IterableDataset`]):
            The iterable dataset to split by node.
        rank (`int`):
            Rank of the current node.
        world_size (`int`):
            Total number of nodes.

    Returns:
        [`IterableDataset`]: The iterable dataset to be used on the node at rank `rank`.
    """
    if dataset._distributed:
        rank = world_size * dataset._distributed.rank + rank
        world_size = world_size * dataset._distributed.world_size
    distributed = DistributedConfig(rank=rank, world_size=world_size)
    return IterableDataset(
        ex_iterable=dataset._ex_iterable,
        info=dataset._info.copy(),
        split=dataset._split,
        formatting=dataset._formatting,
        shuffling=copy.deepcopy(dataset._shuffling),
        distributed=distributed,
        token_per_repo_id=dataset._token_per_repo_id,
    )


async def _apply_async(pool, func, x):
    future = pool.apply_async(func, (x,))
    while True:
        if future.ready():
            return future.get()
        else:
            await asyncio.sleep(0)
