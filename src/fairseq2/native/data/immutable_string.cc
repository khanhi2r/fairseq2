// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include "fairseq2/native/data/immutable_string.h"

#include <algorithm>

#include "fairseq2/native/data/text/detail/utf.h"

using namespace fairseq2::detail;

namespace fairseq2 {

immutable_string::immutable_string(std::string_view s)
  : storage_{copy_string(s)}
{}

std::size_t
immutable_string::get_code_point_length() const
{
    return compute_code_point_length(view());
}

memory_block
immutable_string::copy_string(std::string_view s)
{
    writable_memory_block block = allocate_memory(s.size());

    std::copy(s.begin(), s.end(), block.cast<value_type>().begin());

    return block;
}

std::vector<immutable_string>
immutable_string::split(char separator) const
{
    std::vector<immutable_string> output{};

    split(separator, [&output](immutable_string &&s) {
        output.push_back(std::move(s));
    });

    return output;
}

void
immutable_string::split(
    char separator, const std::function<void(immutable_string &&)> &handler) const
{
    std::string_view s = view();

    std::size_t offset = 0;

    for (std::size_t char_idx = 0; char_idx < s.size(); ++char_idx) {
        if (s[char_idx] == separator) {
            if (offset != char_idx) {
                immutable_string part{storage_.share_slice(offset, char_idx - offset)};

                handler(std::move(part));
            }

            offset = char_idx + 1;
        }
    }

    if (offset != s.size())
        handler(remove_prefix(offset));
}

invalid_utf8_error::~invalid_utf8_error() = default;

}  // namespace fairseq2
