// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include "fairseq2/native/data/record_reader.h"

#include <algorithm>

#include <fmt/core.h>

#include "fairseq2/native/error.h"

using namespace fairseq2::detail;

namespace fairseq2 {

record_reader::~record_reader() = default;

memory_block
record_reader::next()
{
    if (!load_next_record())
        return {};

    memory_block record = extract_record();

    move_to_next_record();

    return record;
}

void
record_reader::reset()
{
    current_chunk_ = {};

    previous_chunks_.clear();

    stream_->reset();
}

bool
record_reader::load_next_record()
{
    record_length_ = 0;

    std::optional<std::size_t> record_end_offset{};

    bool first_chunk = true;

    // Load and store memory chunks until we find the end of the next record.
    while (!(record_end_offset = find_record_end(current_chunk_, first_chunk))) {
        memory_block next_chunk = stream_->read_chunk();
        if (next_chunk.empty()) {
            // If `next_chunk` is empty and we don't have any partial record
            // stored from a previous call, we have reached end of data.
            if (current_chunk_.empty())
                return false;

            throw record_error{
                fmt::format("The stream ends with a partial record of {} byte(s).", current_chunk_.size())};
        }

        // Move `current_chunk_` to previous chunks and attempt to find the record
        // end within `next_chunk` in the next iteration.
        record_length_ += current_chunk_.size();

        previous_chunks_.push_back(std::move(current_chunk_));

        current_chunk_ = std::move(next_chunk);

        first_chunk = false;
    }

    record_length_ += *record_end_offset;

    // The distance to the end of the record within `current_chunk_`.
    record_end_offset_ = *record_end_offset;

    return true;
}

memory_block
record_reader::extract_record()
{
    // If the entire record is contained within `current_chunk_`, just return a
    // reference to it.
    if (previous_chunks_.empty())
        return current_chunk_.share_first(record_length_);

    // Otherwise, merge all previous chunks plus the first `record_end_offset_` bytes
    // of `current_chunk_` into a contiguous memory block.
    return copy_split_record();
}

memory_block
record_reader::copy_split_record()
{
    writable_memory_block record = allocate_memory(record_length_);

    auto iter = record.begin();

    std::for_each(
        previous_chunks_.begin(), previous_chunks_.end(), [&iter](const memory_block &block) {
            iter = std::copy(block.begin(), block.end(), iter);
        });

    std::copy(current_chunk_.begin(), current_chunk_.begin() + record_end_offset_, iter);

    return record;
}

void
record_reader::move_to_next_record()
{
    current_chunk_ = current_chunk_.share_slice(record_end_offset_);

    previous_chunks_.clear();
}

record_error::~record_error() = default;

}  // namespace fairseq2
