# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from numpy.typing import NDArray
from pyarrow.dataset import get_partition_keys  # requires pyarrow >= 13
from tqdm.auto import tqdm

from fairseq2.data import DataPipeline, DataPipelineBuilder, read_sequence
from fairseq2.data.parquet.arrow import pyarrow_column_to_array
from fairseq2.logging import get_log_writer

logger = get_log_writer(__name__)


@contextmanager
def pyarrow_cpu(nb_cpu: int) -> Generator[None, None, None]:
    nb_cpu_old = pa.cpu_count()
    nb_io_cpu_old = pa.io_thread_count()
    pa.set_cpu_count(nb_cpu)
    pa.set_io_thread_count(nb_cpu)
    try:
        yield
    finally:
        pa.set_cpu_count(nb_cpu_old)
        pa.set_io_thread_count(nb_io_cpu_old)


@contextmanager
def torch_random_seed(seed: int | None = None) -> Generator[None, None, None]:
    if seed is not None:
        torch.manual_seed(seed)
    yield


NestedDict = dict[str, "NestedDictValue"]
NestedDictValue = torch.Tensor | list[str] | pd.Series | NestedDict
BatchOutputType = pa.Table | pd.DataFrame | NestedDict


def from_pyarrow_to_torch_tensor(
    arr: pa.Array | pa.ChunkedArray, strict: bool = True
) -> NestedDictValue:
    """
    struct_array = pa.Array.from_pandas([{"x": 4, "y": "RR"}] * 10)
    nest_array = pa.Array.from_pandas([[{'a': 1}, {'a': 2}]])
    """
    # for future ideas https://arrow.apache.org/docs/python/generated/pyarrow.Tensor.html
    # for sparse matrix support https://github.com/apache/arrow/blob/main/python/pyarrow/tests/test_sparse_tensor.py

    if arr.null_count != 0:
        raise ValueError("to torch conversion does not support null values")

    arr = pyarrow_column_to_array(arr)

    arr_type = arr.type
    if pa.types.is_primitive(arr_type):
        try:
            return torch.from_numpy(arr.to_numpy(zero_copy_only=True))
        except Exception:
            pass

    try:
        return torch.from_numpy(arr.to_numpy(zero_copy_only=True))
    except pa.ArrowInvalid:
        pass

    if pa.types.is_dictionary(arr_type):
        return from_pyarrow_to_torch_tensor(arr.dictionary_decode())

    if pa.types.is_string(arr_type):
        return arr.to_pandas().tolist()

    if pa.types.is_list(arr_type) or pa.types.is_large_list(arr_type):
        if pa.types.is_primitive(arr_type.value_type):
            return arr.to_pandas().map(torch.from_numpy).tolist()

        if pa.types.is_fixed_size_list(arr_type.value_type) and pa.types.is_primitive(
            arr_type.value_type.value_type
        ):
            # FIXME: get the column global dtype for empty seq case
            return (
                arr.to_pandas()
                .map(
                    lambda x: torch.from_numpy(
                        np.vstack(x) if len(x) > 0 else np.array([], dtype=np.float32)
                    )
                )
                .tolist()
            )

    if pa.types.is_fixed_size_list(arr_type):
        if pa.types.is_primitive(arr_type.value_type):
            return torch.from_numpy(np.reshape(arr.values, (-1, arr_type.list_size)))

    if pa.types.is_struct(arr_type):
        return {
            arr_type.field(i).name: from_pyarrow_to_torch_tensor(arr.field(i))
            for i in range(arr_type.num_fields)
        }

    if pa.types.is_nested(arr_type):
        # TODO: deal with arr = [[{'a': 1}, {'a': 2}]]
        pass

    if strict:
        raise NotImplementedError(f"{arr_type} cannot be converted to torch.Tensor")
    else:
        return arr  # keeping as in the orignal pyarrow form


def init_parquet_dataset(
    parquet_path: str,
    filters: pa.dataset.Expression | None = None,
    filesystem: pa.fs.FileSystem | None = None,
) -> pq.ParquetDataset:
    return pq.ParquetDataset(parquet_path, filters=filters, filesystem=filesystem)


def get_dataset_fragments(
    dataset: pq.ParquetDataset, filters: pa.dataset.Expression
) -> list[pa.dataset.Fragment]:
    """
    This could be simplified once `split_row_groups=True` is implemented at `pq.ParquetDataset`.
    We could also return a generator instead of list (when getting full infos from S3 may be slow)
    """
    return list(dataset._dataset.get_fragments(filters))


def split_fragment_in_row_groups(
    fragment: pa.dataset.Fragment,
) -> list[pa.dataset.Fragment]:
    return list(fragment.split_by_row_group())


def add_partitioning_values(
    table: pa.Table, fragment: pa.dataset.Fragment, columns: list[str] | None
) -> pa.Table:
    """
    When loading a single fragment, pyarrow does not add the partitioning columns,
    so we need to do it manually.
    """
    for key, val in get_partition_keys(fragment.partition_expression).items():
        if columns is None or key in columns:
            values = pa.DictionaryArray.from_arrays(
                np.zeros(len(table), dtype=np.int32), [val]
            )
            table = table.append_column(key, values)
    return table


def load_one_fragment(
    fragment: pa.dataset.Fragment, columns: list[str] | None = None
) -> pa.Table:
    fragment_columns = columns
    if fragment_columns is not None:
        fragment_columns = [
            col for col in fragment_columns if col in fragment.physical_schema.names
        ]
    fragment_table = fragment.to_table(columns=fragment_columns, use_threads=False)
    fragment_table = add_partitioning_values(fragment_table, fragment, columns)
    return fragment_table


def apply_filter(
    table: pa.Table,
    filters: list[Any] | pa.dataset.Expression | None = None,
    drop_null: bool = True,
) -> pa.Table:
    if drop_null:
        table = table.drop_null()
    if filters is not None:
        table = table.filter(filters)
    return table


def concat_table(tables: list[pa.Table], combine: bool = True) -> pa.Table:
    result = pa.concat_tables(
        tables,
        promote_options="permissive",  # needed to get deal with empty segments
    )
    if combine:
        result = result.combine_chunks()
    return result


def compute_length_splits(
    length_col: NDArray[np.int32], max_tokens: int
) -> list[NDArray[np.int32]]:
    """split sequence of length_col in the chunks such that total length is ~ max_tokens
        countint the padding to max length of elements in a chunk

    Args:
        length_col (np.ndarray):
        max_tokens (int):

    Returns:
        list[np.ndarray]: splits that contain indices over the original length_col
    """
    argsort_ind = np.argsort(length_col)
    # TODO: remove 0 lengths
    sorted_length_col = length_col[argsort_ind]

    splits = []
    ptr = 0
    for i, length in enumerate(sorted_length_col):
        if length * (i - ptr) > max_tokens:
            splits.append(argsort_ind[ptr : (i - 1)])
            ptr = i - 1
    if (
        length <= max_tokens
    ):  # we drop the last iteration if it results in a batch greater than max_tokens
        splits.append(argsort_ind[ptr:])
    return splits


def compute_rows_length(pa_array: pa.Array) -> NDArray[np.int32]:
    type_ = pa_array.type
    if pa.types.is_list(type_) or pa.types.is_large_list(type_):
        length_col = pa.compute.list_value_length(pa_array).to_numpy()
    elif pa.types.is_string(type_):
        length_col = pa.compute.utf8_length(pa_array).to_numpy()
    else:
        length_col = np.asarray(pa_array.to_pandas().apply(len))

    length_col = length_col.copy()
    length_col[np.isnan(length_col)] = 0
    return np.asarray(length_col, dtype=np.int32)


class _TableWrapper:
    """
    class to avoid fairseq2 casting pa.Table to iterable objects
    which currently fails
    """

    def __init__(self, table: pa.Table) -> None:
        self.table: pa.Table = table


def _to_real_object(x: _TableWrapper | NestedDict) -> BatchOutputType:
    if isinstance(x, _TableWrapper):
        return x.table
    elif isinstance(x, list):
        return [_to_real_object(e) for e in x]
    elif isinstance(x, tuple):
        return tuple(_to_real_object(e) for e in x)
    else:
        return x


def table_func_wrap(func):  # type: ignore
    def inner(*args):  # type: ignore
        fixed_args = [_to_real_object(x) for x in args]
        result = func(*fixed_args)
        if isinstance(result, (pa.Table, pd.DataFrame)):
            result = _TableWrapper(result)
        return result

    return inner


def list_parquet_fragments(
    parquet_path: str,
    filters: pa.dataset.Expression | None = None,
    columns: list[str] | None = None,
    split_to_row_groups: bool = True,
    filesystem: pa.fs.FileSystem | None = None,
    shuffle_window: int | None = None,
    seed: int = 2,
) -> DataPipelineBuilder:
    dataset = init_parquet_dataset(parquet_path, filters=filters, filesystem=filesystem)
    columns = columns or dataset.schema.names
    if not set(columns).issubset(set(dataset.schema.names)):
        raise ValueError(
            f"columns {sorted(set(columns) - set(dataset.schema.names))} are not found in the dataset schema"
        )

    pipeline_builder = read_sequence(get_dataset_fragments(dataset, filters))

    if shuffle_window is not None:
        # shuffle them in full memory since fragments are already known
        pipeline_builder = pipeline_builder.shuffle(shuffle_window=0, seed=seed)

    if split_to_row_groups:
        pipeline_builder = pipeline_builder.yield_from(
            lambda fragment: read_sequence(
                split_fragment_in_row_groups(fragment)
            ).and_return()
        )
        if shuffle_window is not None:
            pipeline_builder = pipeline_builder.shuffle(
                shuffle_window=shuffle_window, seed=seed + 1
            )

    return pipeline_builder


def build_iterator_over_one_table(
    table: pa.Table,
    order_by_length: str | None = None,
    batch_size: int | None = None,
    max_tokens: int | None = None,
    shuffle: bool = True,
    seed: int | None = None,
    num_parallel_calls: int = 8,
) -> DataPipeline:
    random_state = np.random.RandomState(seed)
    if order_by_length is not None:
        length_col = compute_rows_length(table[order_by_length])
        # add small perturbation to avoid same sample appear together during different epochs
        if shuffle:
            perturbation = random_state.randint(
                0,
                np.quantile(length_col, 0.001).astype(np.int32) + 2,
                len(length_col),
            )
            length_col += np.asarray(perturbation, dtype=np.int32)
    else:
        if shuffle:
            length_col = random_state.randint(0, 2**23, len(table))
        else:
            length_col = np.zeros(len(table), dtype=np.int32)

    if batch_size is not None:
        order_tt = pa.Table.from_arrays(
            [pa.array(np.argsort(length_col, kind="stable"))], ["order"]
        )
        batches = [ind["order"] for ind in order_tt.to_batches(batch_size)]
    elif max_tokens is not None:
        batches = compute_length_splits(length_col, max_tokens)
    else:
        raise ValueError("unknown batching method")

    if shuffle:
        batches = [batches[i] for i in random_state.permutation(len(batches))]

    return (
        read_sequence(batches)
        .map(
            table_func_wrap(lambda ind: table.take(ind).combine_chunks()),
            num_parallel_calls=num_parallel_calls,
        )
        .and_return(max_num_warnings=4)
    )


def get_row_group_level_metadata(
    dataset: pq.ParquetDataset,
    columns: Optional[List[str]] = None,
    nb_jobs: int = 40,
    max_fragments: int = -1,
    seed: int = 123,
) -> pd.DataFrame:
    """
    Parses row group level metadata from a Parquet dataset and returns it as a pandas DataFrame.
    It's similar to `get_parquet_dataset_metadata`
    but present a unnested view on row groups statistics for only a subset of columns.
    This function can be used for any kind of downstream analysis.

    It uses joblib for parallel processing
    and tqdm for progress tracking, which are good practices for handling large datasets.

    Parameters:
    - dataset (pq.ParquetDataset): The Parquet dataset to parse.
    - columns (list of str, optional): The columns to include in the output DataFrame. If not specified, all columns are included.
                For `columns=[]` no column-vise information will be profided (which is generally much faster).
    - nb_jobs (int, default=40): The number of parallel jobs to run.
    - max_fragments (int, default=-1): The maximum number of fragments to include. If -1, all fragments are included.
    - seed (int, default=123): The seed for the random number generator, used when selecting fragments.

    Returns:
    - pd.DataFrame: A DataFrame containing the row group level metadata.
    Example:
        >>> import pyarrow as pa
        >>> import pyarrow.fs
        >>> import pyarrow.compute as pc
        >>> fs, parquet_uri = pa.fs.FileSystem.from_uri("s3://<bucket_name>/<dataset_name>/")
        >>> dataset = pq.ParquetDataset(parquet_uri, filesystem=fs, filters=pc.equal(pc.field("split"), "validation"))
        >>> df_stats = get_row_group_level_metadata(dataset, columns=["col1", "col2", ...])
    """
    assert max_fragments >= -1
    fragments = list(dataset._dataset.get_fragments(filter=dataset._filter_expression))

    if max_fragments != -1 and max_fragments < len(fragments):
        fragments = (
            np.random.RandomState(seed)
            .choice(np.array(fragments, dtype="O"), max_fragments, replace=False)
            .tolist()
        )

    physical_schema = fragments[0].physical_schema

    columns = columns if columns is not None else physical_schema.names
    # taking only existing columns
    non_existing_columns = tuple(set(columns) - set(physical_schema.names))
    if non_existing_columns:
        print(
            "Following colums are not present in physical schema and will be ignored",
            non_existing_columns,
        )
    columns = [col for col in columns if col in physical_schema.names]

    columns_index = [physical_schema.get_field_index(col) for col in columns]

    columns_to_exclude = set(["row_group_id", "num_rows", "total_byte_size"]) & set(
        columns
    )
    assert (
        len(columns_to_exclude) == 0
    ), f"names conflict, rename/remove : {columns_to_exclude}"

    def get_one_row_group_stats(row_group: pa.dataset.RowGroup) -> dict:
        metadata = row_group.metadata
        info = {
            "row_group_id": row_group.id,
            "num_rows": metadata.num_rows,
            "total_byte_size": metadata.total_byte_size,
        }
        for col, ind in zip(columns, columns_index):
            info[col] = metadata.column(ind).to_dict()
        return info

    def get_fragment_stats(frag: pa.dataset.Fragment) -> dict:
        return {
            "rg_stats": list(map(get_one_row_group_stats, frag.row_groups)),
            "parquet_file_path": frag.path,
            **get_partition_keys(frag.partition_expression),
        }

    stats = joblib.Parallel(nb_jobs, backend="threading")(
        joblib.delayed(get_fragment_stats)(frag) for frag in tqdm(fragments)
    )

    stats = pd.DataFrame(stats).explode("rg_stats")
    flatten_row_df = pd.DataFrame(stats.pop("rg_stats").tolist(), index=stats.index)
    result_df = pd.concat([stats, flatten_row_df], axis=1)
    return result_df


def get_parquet_dataset_metadata(
    dataset: pq.ParquetDataset,
    full: bool = True,
    nb_jobs: int = 40,
    max_fragments: int = -1,
    seed: int = 123,
) -> pd.DataFrame:
    """
    Extracts metadata from a Parquet dataset.
    Parameters:
    - dataset (pq.ParquetDataset): The Parquet dataset to extract metadata from.
    - full (bool, optional): If True, extracts full stats. If False, extracts minimal stats to speedup the process. Defaults to True.
    - nb_jobs (int, optional): The number of jobs to run in parallel. Defaults to 40.
    - max_fragments (int, optional): The maximum number of fragments to process. If -1, all fragments are processed. Defaults to -1.
    - seed (int, optional): The seed for the random number generator used when selecting fragments. Defaults to 123.

    Returns:
    pd.DataFrame: A DataFrame containing the extracted metadata.

    Example:
        >>> import pyarrow as pa
        >>> import pyarrow.fs
        >>> import pyarrow.compute as pc
        >>> fs, parquet_uri = pa.fs.FileSystem.from_uri("s3://<bucket_name>/<dataset_name>/")
        >>> dataset = pq.ParquetDataset(parquet_uri, filesystem=fs, filters=pc.equal(pc.field("split"), "train"))
        >>> df_stats = get_parquet_dataset_metadata(dataset, full=True)
        >>> df_stats.explode("row_groups")  # to see row groups level info

    """
    assert max_fragments >= -1
    fragments = list(dataset._dataset.get_fragments(filter=dataset._filter_expression))

    if max_fragments != -1 and max_fragments < len(fragments):
        fragments = (
            np.random.RandomState(seed)
            .choice(np.array(fragments, dtype="O"), max_fragments, replace=False)
            .tolist()
        )

    def get_fragment_full_stats(frag: pa.dataset.Fragment) -> dict:
        return {
            "parquet_file_path": frag.path,
            **frag.metadata.to_dict(),
            **get_partition_keys(frag.partition_expression),
        }

    def get_fragment_minimal_stats(frag: pa.dataset.Fragment) -> dict:
        meta = frag.metadata
        return {
            "parquet_file_path": frag.path,
            "num_row_groups": frag.num_row_groups,
            "num_rows": frag.count_rows(),
            "num_columns": meta.num_columns,
            "serialized_size": meta.serialized_size,
            **get_partition_keys(frag.partition_expression),
        }

    stats_fn = get_fragment_full_stats if full else get_fragment_minimal_stats
    stats = joblib.Parallel(nb_jobs, backend="threading")(
        joblib.delayed(stats_fn)(frag) for frag in tqdm(fragments)
    )

    df_stats = pd.DataFrame(stats)
    return df_stats


def pyarrow_table_to_torch_dict(tt: pa.Table, strict: bool = False) -> NestedDict:
    out = {}
    for col in tt.column_names:
        try:
            out[col] = from_pyarrow_to_torch_tensor(tt[col], strict)
        except ValueError as e:
            logger.info(
                f"Column {col} of type {tt[col].type} was not converted to torch as expected",
                str(e),
            )
            out[col] = tt[col]
    return out
