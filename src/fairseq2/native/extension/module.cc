// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include "fairseq2/native/extension/module.h"

#include <fairseq2/native/config.h>

namespace py = pybind11;

namespace fairseq2 {

PYBIND11_MODULE(C, m)
{
    py::options opts{};
    opts.disable_function_signatures();

    m.def(
        "_supports_cuda",
        []
        {
          return supports_cuda;
        });

// See https://github.com/llvm/llvm-project/issues/57123.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wunreachable-code-return"

    m.def(
        "_cuda_version",
        []
        {
          if constexpr (cuda_version_major)
              return py::make_tuple(*cuda_version_major, *cuda_version_minor);
          else
              return py::none();
        });

#pragma clang diagnostic pop

    def_data(m);
}

}  // namespace fairseq2
