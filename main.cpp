#include "aggregate.hpp"
#include "events.hpp"
#include "output.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/resource.h>

namespace {
const char* setting(const char* key, const char* fallback) {
    const char* value = std::getenv(key);
    return value && *value ? value : fallback;
}

bool prefer_ordered(const std::filesystem::path& data, unsigned threads) {
    std::string_view mode = setting("SHOP_READER", "auto");
    if (mode == "ordered") return true;
    if (mode == "stream") return false;
    if (mode != "auto") throw std::invalid_argument("SHOP_READER must be auto, ordered or stream");
    // The mapped input competes with row indexes and aggregation for RAM.
    // Bound mapping both per worker and relative to the advertised memory limit.
    constexpr uint64_t mib = uint64_t{1} << 20;
    uint64_t budget = threads * 256 * mib;
    if (const char* memory = std::getenv("MEM_GB")) {
        char* end = nullptr;
        long gib = std::strtol(memory, &end, 10);
        if (end != memory && !*end && gib > 0 && gib <= 1024)
            budget = std::min(budget, static_cast<uint64_t>(gib) * 1024 * mib / 3);
    }
    for (const auto& file : std::filesystem::directory_iterator(data / "events")) {
        if (file.path().extension() != ".csv") continue;
        auto bytes = file.file_size();
        if (bytes > budget) return false;
        budget -= bytes;
    }
    return true;
}
}

int main(int argc, char** argv) {
    try {
        if (argc != 1 && argc != 3) {
            std::cerr << "usage: shop [input-directory output-directory]\n";
            return 2;
        }
        const char* data = argc == 3 ? argv[1] : setting("DATA_DIR", "/data");
        const char* output = argc == 3 ? argv[2] : setting("OUTPUT_DIR", "/output");
        const unsigned threads = std::clamp(std::stoi(setting("NCPU", "8")), 1, 64);
        auto last = std::chrono::steady_clock::now();
        const bool profile = std::getenv("SHOP_PROFILE") != nullptr;
        rusage previous{};
        if (profile) getrusage(RUSAGE_SELF, &previous);
        auto checkpoint = [&](const char* stage) {
            if (!profile) return;
            auto now = std::chrono::steady_clock::now();
            rusage usage{};
            getrusage(RUSAGE_SELF, &usage);
            std::cerr << stage << " " << std::chrono::duration<double>(now - last).count() << " s"
                      << " | faults " << usage.ru_minflt - previous.ru_minflt << " minor, "
                      << usage.ru_majflt - previous.ru_majflt << " major\n";
            previous = usage;
            last = now;
        };
        std::unique_ptr<shop::Inputs> inputs;
        std::vector<shop::WorkerEvents> workers(threads);
        if (prefer_ordered(data, threads)) {
            inputs = shop::read_ordered_events(data, workers);
            checkpoint("ordered events, links and clicks");
        } else {
            inputs = std::make_unique<shop::Inputs>(data);
            checkpoint("dimensions, links, clicks");
            shop::read_events(data, *inputs, workers);
            checkpoint("stream events");
        }
        auto totals = shop::aggregate(*inputs, workers, inputs->clicks, threads);
        checkpoint("session and campaign aggregation");
        shop::write_output(output, *inputs, totals);
        checkpoint("parquet");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "shop: " << error.what() << '\n';
        return 1;
    }
}
