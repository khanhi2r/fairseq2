// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.
//#define _GLIBCXX_USE_TBB_PAR_BACKEND 1

#include "fairseq2/native/data/text/sentencepiece/sp_encoder.h"

#include <cstddef>
#include <stdexcept>

#include <ATen/Functions.h>
#include <ATen/Storage.h>

#include "fairseq2/native/memory.h"
#include "fairseq2/native/span.h"
#include "fairseq2/native/data/immutable_string.h"
#include "fairseq2/native/data/text/sentencepiece/sp_model.h"
#include "fairseq2/native/data/text/sentencepiece/sp_processor.h"
#include "fairseq2/native/utils/cast.h"

using sentencepiece::ImmutableSentencePieceText;

namespace fairseq2 {
namespace detail {

class encoder_op {
public:
    explicit
    encoder_op(
        const sp_encoder *encoder, const sp_processor *processor, immutable_string &&sentence);

    at::Tensor &&
    run() &&;

private:
    void
    encode_string();

    void
    fill_tensor();

private:
    const sp_encoder *encoder_;
    const sp_processor *processor_;
    immutable_string sentence_;
    ImmutableSentencePieceText spt_{};
    std::size_t extra_tokens_len_{};
    std::size_t seq_len_{};
    at::Tensor tensor_{};
};

}  // namespace detail

sp_encoder::sp_encoder(std::shared_ptr<const sp_model> model, sp_encoder_options opts)
  : model_{std::move(model)}, opts_{std::move(opts)}
{
    prefix_token_indices_.reserve(opts_.prefix_tokens().size());
    suffix_token_indices_.reserve(opts_.suffix_tokens().size());

    for (const std::string &token : opts_.prefix_tokens())
        prefix_token_indices_.push_back(model_->token_to_index(token));

    for (const std::string &token : opts_.suffix_tokens())
        suffix_token_indices_.push_back(model_->token_to_index(token));
}

data
sp_encoder::operator()(data &&d) const
{
    if (!d.is_string())
        throw std::invalid_argument{"The input data must be of type string."};

    return encode(std::move(d).as_string());
}

at::Tensor
sp_encoder::encode(immutable_string &&sentence) const
{
    detail::encoder_op op{this, model_->processor_.get(), std::move(sentence)};

    return std::move(op).run();
}

namespace detail {
namespace {

std::int64_t
get_token_idx(const ImmutableSentencePieceText &spt, std::size_t idx) noexcept
{
    std::uint32_t id = spt.pieces(conditional_cast<int>(idx)).id();

    return static_cast<std::int64_t>(id);
}

}  // namespace

encoder_op::encoder_op(
    const sp_encoder *encoder, const sp_processor *processor, immutable_string &&sentence)
  : encoder_{encoder}, processor_{processor}, sentence_{std::move(sentence)}
{
    extra_tokens_len_ += encoder_->prefix_token_indices_.size();
    extra_tokens_len_ += encoder_->suffix_token_indices_.size();
}

at::Tensor &&
encoder_op::run() &&
{
    encode_string();

    tensor_ = at::zeros({static_cast<std::int64_t>(seq_len_)},
        at::dtype(at::kLong).device(at::kCPU).pinned_memory(encoder_->opts_.pin_memory()));

    fill_tensor();

    std::optional<at::Device> device = encoder_->opts_.device();
    if (device)
        tensor_ = tensor_.to(*device);

    return std::move(tensor_);
}

void
encoder_op::encode_string()
{
    auto &opts = encoder_->opts_;

    if (opts.enable_sampling())
        spt_ = processor_->sample(sentence_, opts.nbest_size(), opts.alpha());
    else
        spt_ = processor_->encode(sentence_);

    seq_len_ = spt_.pieces_size() + extra_tokens_len_;
}

void
encoder_op::fill_tensor()
{
    const at::Storage &storage = tensor_.storage();

    writable_memory_span tensor_bits{storage.unsafe_data<std::byte>(), storage.nbytes()};

    span tensor_data = cast<std::int64_t>(tensor_bits);

    if (encoder_->opts_.reverse()) {
        std::size_t i = seq_len_ - 1;

        for (std::int64_t prefix_idx : encoder_->prefix_token_indices_)
            tensor_data[i--] = prefix_idx;

        for (std::size_t j = 0; j < spt_.pieces_size(); ++j)
            tensor_data[i--] = get_token_idx(spt_, j);

        for (std::int64_t suffix_idx : encoder_->suffix_token_indices_)
            tensor_data[i--] = suffix_idx;
    } else {
        std::size_t i = 0;

        for (std::int64_t prefix_idx : encoder_->prefix_token_indices_)
            tensor_data[i++] = prefix_idx;

        for (std::size_t j = 0; j < spt_.pieces_size(); ++j)
            tensor_data[i++] = get_token_idx(spt_, j);

        for (std::int64_t suffix_idx : encoder_->suffix_token_indices_)
            tensor_data[i++] = suffix_idx;
    }
}

}  // namespace detail
}  // namespace fairseq2
