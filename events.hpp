#pragma once

#include "aggregate.hpp"

#include <filesystem>
#include <memory>
#include <string>
#include <vector>

namespace shop {

std::unique_ptr<Inputs> read_ordered_events(const std::string& data, std::vector<WorkerEvents>& workers);

// Scan event CSVs into one buffer per worker, applying global earliest-ingestion deduplication.
void read_events(const std::filesystem::path& data_dir, const Inputs& inputs,
                 std::vector<WorkerEvents>& workers);

} // namespace shop
