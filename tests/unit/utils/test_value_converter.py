# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import torch

from fairseq2.typing import DataType
from fairseq2.utils.dataclass import EMPTY
from fairseq2.utils.structured import StructuredError, ValueConverter

# mypy: disable-error-code="arg-type"


class FooEnum(Enum):
    VALUE1 = 1
    VALUE2 = 2


@dataclass
class Foo1:
    f0: str = "foo"
    f1: int = 0
    f2: dict[str, Path] = field(default_factory=dict)
    f3: list[int] = field(default_factory=list)
    f4: Foo2 | Foo3 = field(default_factory=lambda: Foo2())
    f5: tuple[float, int] = (1.0, 2)
    f6: set[int] = field(default_factory=set)
    f7: FooEnum | None = None
    f8: DataType = torch.float32
    f9: Foo3 | None = None


@dataclass
class Foo2:
    f2_1: int = 1


@dataclass
class Foo3:
    f3_1: int = 2
    f3_2: int = 3


class TestValueConverter:
    def test_structure_works(self) -> None:
        data = {
            "f0": "abc",
            "f1": "2",
            "f2": {"a": "path1", "b": Path("path2")},
            "f3": [0, "1", 2, "3"],
            "f4": {"f3_1": "4"},
            "f5": ["3.0", "4"],
            "f6": ["1", "2", "3"],
            "f7": "VALUE2",
            "f8": "~~",
            "f9": {"f3_2": "4"},
        }

        value_converter = ValueConverter()

        foo = value_converter.structure(data, Foo1, allow_empty=True)

        expected_foo = Foo1(
            f0="abc",
            f1=2,
            f2={"a": Path("path1"), "b": Path("path2")},
            f3=[0, 1, 2, 3],
            f4=Foo3(f3_1=4),
            f5=(3.0, 4),
            f6={1, 2, 3},
            f7=FooEnum.VALUE2,
            f8=EMPTY,
            f9=Foo3(f3_2=4),
        )

        assert foo == expected_foo

    def test_structure_raises_error_when_field_is_empty(self) -> None:
        data: Any

        data = {"f0": "~~"}

        value_converter = ValueConverter()

        with pytest.raises(
            StructuredError, match=rf"^`obj` cannot be structured to `{Foo1}`\. See nested exception for details\.$"  # fmt: skip
        ):
            value_converter.structure(data, Foo1)

        data = {"f4": {"f3_1": "~~"}}

        with pytest.raises(
            StructuredError, match=rf"^`obj` cannot be structured to `{Foo1}`\. See nested exception for details\.$"  # fmt: skip
        ):
            value_converter.structure(data, Foo1)

    @pytest.mark.parametrize(
        "data,kls",
        [
            ("a", int),
            ({"a": 1}, dict),
            ("a", list),
            ("a", FooEnum),
            ({"f1_1": 2, "f1_2": 3}, Foo2),
        ],
    )
    def test_structure_raises_error_when_conversion_fails(
        self, data: Any, kls: type
    ) -> None:
        value_converter = ValueConverter()

        with pytest.raises(
            StructuredError, match=rf"^`obj` cannot be structured to `{kls}`\. See nested exception for details\.$"  # fmt: skip
        ):
            value_converter.structure(data, kls)

    def test_unstructure_works(self) -> None:
        foo = Foo1(
            f0="abc",
            f1=2,
            f2={"a": Path("path1"), "b": Path("path2")},
            f3=[0, 1, 2, 3],
            f4=Foo3(f3_1=4),
            f5=(3.0, 4),
            f6={1, 2, 3},
            f7=FooEnum.VALUE2,
            f8=torch.float16,
            f9=Foo3(f3_1=1),
        )

        value_converter = ValueConverter()

        data = value_converter.unstructure(foo)

        expected_data = {
            "f0": "abc",
            "f1": 2,
            "f2": {"a": "path1", "b": "path2"},
            "f3": [0, 1, 2, 3],
            "f4": {"f3_1": 4, "f3_2": 3},
            "f5": [3.0, 4],
            "f6": [1, 2, 3],
            "f7": "VALUE2",
            "f8": "float16",
            "f9": {"f3_1": 1, "f3_2": 3},
        }

        assert data == expected_data
