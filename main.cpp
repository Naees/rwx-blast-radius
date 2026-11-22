#include "aggregate.hpp"
#include "events.hpp"
#include "output.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <sys/resource.h>

namespace {
const char* setting(const char* key, const char* fallback) {
    const char* value = std::getenv(key);
    return value && *value ? value : fallback;
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
        shop::Inputs inputs(data);
        checkpoint("dimensions, links, clicks");
        std::vector<shop::WorkerEvents> workers(threads);
        shop::read_events(data, inputs, workers);
        checkpoint("events");
        auto totals = shop::aggregate(inputs, workers, inputs.clicks, threads);
        checkpoint("session and campaign aggregation");
        shop::write_output(output, inputs, totals);
        checkpoint("parquet");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "shop: " << error.what() << '\n';
        return 1;
    }
}
