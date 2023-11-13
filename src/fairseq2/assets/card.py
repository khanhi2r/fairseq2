# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import os
from typing import (
    AbstractSet,
    Any,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Type,
    TypeVar,
    cast,
)
from urllib.parse import urlparse

from typing_extensions import Self

from fairseq2.assets.error import AssetError

T = TypeVar("T")


class AssetCard:
    """Holds information about an asset."""

    name: str
    data: MutableMapping[str, Any]
    base: Optional[AssetCard]

    def __init__(
        self,
        name: str,
        data: MutableMapping[str, Any],
        base: Optional[AssetCard] = None,
    ) -> None:
        """
        :param name:
            The name of the asset.
        :param data:
            The data to be held in the card. Each key-value entry (i.e. field)
            should contain a specific piece of information about the asset.
        :param base:
            The card that this card derives from.
        """
        self.name = name
        self.data = data
        self.base = base

    def field(self, name: str) -> AssetCardField:
        """Return a field of this card.

        If the card does not contain the specified field, its base card will be
        checked recursively.

        :param name:
            The name of the field.
        """
        return AssetCardField(self, [name])

    def _get_field_value(self, name: str, path: List[str]) -> Any:
        assert len(path) > 0

        data = self.data

        contains = True

        for field in path:
            if data is None:
                contains = False

                break

            if not isinstance(data, Mapping):
                pathname = ".".join(path)

                raise AssetCardFieldNotFoundError(
                    f"The asset card '{name}' must have a field named '{pathname}'."
                )

            try:
                data = data[field]
            except KeyError:
                contains = False

                break

        if not contains:
            if self.base is not None:
                return self.base._get_field_value(name, path)

            pathname = ".".join(path)

            raise AssetCardFieldNotFoundError(
                f"The asset card '{name}' must have a field named '{pathname}'."
            )

        return data

    def _set_field_value(self, path: List[str], value: Any) -> None:
        assert len(path) > 0

        data = self.data

        for depth, field in enumerate(path[:-1]):
            try:
                data = data[field]
            except KeyError:
                tmp: Dict[str, Any] = {}

                data[field] = tmp

                data = tmp

            if not isinstance(data, Mapping):
                conflict_pathname = ".".join(path[: depth + 1])

                pathname = ".".join(path)

                raise AssetCardError(
                    f"The asset card '{self.name}' cannot have a field named '{pathname}' due to path conflict at '{conflict_pathname}'."
                )

        data[path[-1]] = value

    def __str__(self) -> str:
        return str(self.data)


class AssetCardField:
    """Represents a field of an asset card."""

    card: AssetCard
    path: List[str]

    def __init__(self, card: AssetCard, path: List[str]) -> None:
        """
        :param card:
            The card owning this field.
        :param path:
            The path to this field in the card.
        """
        self.card = card
        self.path = path

    def field(self, name: str) -> AssetCardField:
        """Return a sub-field of this field.

        :param name:
            The name of the sub-field.
        """
        return AssetCardField(self.card, self.path + [name])

    def is_none(self) -> bool:
        """Return ``True`` if the field is ``None``."""
        value = self.card._get_field_value(self.card.name, self.path)

        return value is None

    def as_(self, kls: Type[T], allow_empty: bool = False) -> T:
        """Return the value of this field.

        :param kls:
            The type of the field.
        :param allow_empty:
            If ``True``, allows the field to be empty.
        """
        value = self.card._get_field_value(self.card.name, self.path)
        if value is None:
            pathname = ".".join(self.path)

            raise AssetCardError(
                f"The value of the field '{pathname}' of the asset card '{self.card.name}' must not be `None`."
            )

        if not isinstance(value, kls):
            pathname = ".".join(self.path)

            raise AssetCardError(
                f"The value of the field '{pathname}' of the asset card '{self.card.name}' must be of type `{kls}`, but is of type `{type(value)}` instead."
            )

        if not allow_empty and not value:
            pathname = ".".join(self.path)

            raise AssetCardError(
                f"The value of the field '{pathname}' of the asset card '{self.card.name}' must not be empty."
            )

        return value

    def as_uri(self) -> str:
        """Return the value of this field as a URI."""
        value = self.as_(str)

        try:
            uri = urlparse(value)
        except ValueError:
            uri = None

        if uri and uri.scheme and uri.netloc:
            return value

        pathname = ".".join(self.path)

        raise AssetCardError(
            f"The value of the field '{pathname}' of the asset card '{self.card.name}' must be a URI, but is '{value}' instead."
        )

    def as_filename(self) -> str:
        """Return the value of this field as a filename."""
        value = self.as_(str)

        if os.sep in value or (os.altsep and os.altsep in value):
            pathname = ".".join(self.path)

            raise AssetCardError(
                f"The value of the field '{pathname}' of the asset card '{self.card.name}' must be a filename, but is '{value}' instead."
            )

        return value

    def as_list(self, kls: Type[T], allow_empty: bool = False) -> List[T]:
        """Return the value of this field as a :class:`list` of type ``kls``.

        :param kls:
            The type of the field elements.
        :param allow_empty:
            If ``True``, allows the list to be empty.
        """
        value = self.as_(list, allow_empty)

        for element in value:
            if not isinstance(element, kls):
                pathname = ".".join(self.path)

                raise AssetCardError(
                    f"The elements of the field '{pathname}' of the asset card '{self.card.name}' must be of type `{kls}`, but at least one element is of type `{type(element)}` instead."
                )

        return value

    def as_one_of(self, valid_values: AbstractSet[T]) -> T:
        """Return the value of this field as one of the values in ``valid_values``

        :param values:
            The values to check against.
        """
        value = self.as_(object)

        if value in valid_values:
            return cast(T, value)

        pathname = ".".join(self.path)

        values = list(valid_values)

        values.sort()

        raise AssetCardError(
            f"The value of the field '{pathname}' of the asset card '{self.card.name}' must be one of {values}, but is {repr(value)} instead."
        )

    def set(self, value: Any) -> None:
        """Set the value of this field."""
        self.card._set_field_value(self.path, value)

    def check_equals(self, value: Any) -> Self:
        """Check if the value of this field equals to ``value``."""
        if (v := self.as_(object)) != value:
            pathname = ".".join(self.path)

            raise AssetCardError(
                f"The value of the field '{pathname}' of the asset card '{self.card.name}' must be '{value}', but is {repr(v)} instead."
            )

        return self


class AssetCardError(AssetError):
    """Raised when an asset card cannot be processed."""


class AssetCardFieldNotFoundError(AssetCardError):
    """Raised when an asset card field cannot be found."""
