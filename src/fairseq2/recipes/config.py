# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar, final

from fairseq2.typing import DataClass
from fairseq2.utils.structured import StructuredError, ValueConverter

ConfigT = TypeVar("ConfigT", bound=DataClass)


class ConfigurationManager(ABC):
    @abstractmethod
    def get_section(self, name: str, kls: type[ConfigT]) -> ConfigT:
        ...

    @abstractmethod
    def get_optional_section(self, name: str, kls: type[ConfigT]) -> ConfigT | None:
        ...


@final
class StandardConfigurationManager(ConfigurationManager):
    _value_converter: ValueConverter
    _unstructured_config: dict[str, object]

    def __init__(
        self, value_converter: ValueConverter, unstructured_config: dict[str, object]
    ) -> None:
        self._value_converter = value_converter
        self._unstructured_config = unstructured_config

    def get_section(self, name: str, kls: type[ConfigT]) -> ConfigT:
        try:
            unstructured_section = self._unstructured_config[name]
        except KeyError:
            raise SectionNotFoundError(
                f"The '{name}' configuration section is not found."
            ) from None

        try:
            return self._value_converter.structure(unstructured_section, kls)  # type: ignore[no-any-return]
        except StructuredError as ex:
            raise ConfigurationError(
                f"The '{name}' configuration section cannot be parsed. See nested exception for detail."
            ) from ex

    def get_optional_section(self, name: str, kls: type[ConfigT]) -> ConfigT | None:
        try:
            return self.get_section(name, kls)
        except SectionNotFoundError:
            return None


class ConfigurationError(RuntimeError):
    pass


class SectionNotFoundError(ConfigurationError):
    pass
