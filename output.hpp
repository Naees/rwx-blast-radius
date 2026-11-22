#pragma once

#include "aggregate.hpp"
#include <filesystem>

namespace shop {
void write_output(const std::filesystem::path& root, const Inputs& inputs, const DailyTables& tables);
}
