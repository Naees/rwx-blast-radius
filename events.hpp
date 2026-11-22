#pragma once

#include "aggregate.hpp"

#include <filesystem>
#include <vector>

namespace shop {

// Scan event CSVs into one buffer per worker, applying global earliest-ingestion deduplication.
void read_events(const std::filesystem::path& data_dir, const Inputs& inputs,
                 std::vector<WorkerEvents>& workers);

} // namespace shop
