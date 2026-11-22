#include "aggregate.hpp"
#include "events.hpp"
#include "output.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdlib>
#include <future>
#include <iostream>
#include <memory>
#include <string>
#include <sys/resource.h>

namespace {
struct alignas(128) PendingEvents {
    static constexpr size_t capacity = shop::Inputs::lookup_batch_size;
    std::array<std::string_view, capacity> names;
    std::array<shop::Event, capacity> events;
    size_t count = 0;
    void flush(const shop::Inputs& inputs, shop::WorkerEvents& worker) {
        std::array<uint32_t, capacity> resolved;
        inputs.resolve_people(names.data(), resolved.data(), count);
        for (size_t i = 0; i < count; ++i) {
            events[i].person = resolved[i];
            worker.add(inputs.window_start, events[i]);
        }
        count = 0;
    }
};

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
        // Link resolution and click loading are independent of event-ID deduplication.
        double inputs_seconds = 0;
        auto loading = std::async(std::launch::async, [&] {
            auto start = profile ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
            auto loaded = std::make_unique<shop::Inputs>(data);
            if (profile) inputs_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
            return loaded;
        });
        std::unique_ptr<shop::Inputs> inputs;
        std::vector<shop::WorkerEvents> workers(threads);
        {
            auto start = profile ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
            shop::EventBatch batch(data, threads);
            auto indexed = profile ? std::chrono::steady_clock::now() : start;
            inputs = loading.get();
            if (profile) {
                std::cerr << "input initialization " << inputs_seconds << " s"
                          << " | event indexing " << std::chrono::duration<double>(indexed - start).count() << " s\n";
            }
            checkpoint("dimensions, clicks, event deduplication");
            const size_t per_worker = batch.row_count() / threads + 1;
            for (auto& worker : workers) worker.reserve(inputs->person_count(), per_worker);
            std::vector<PendingEvents> pending(threads);
            batch.consume(inputs->window_start, inputs->window_end,
                [&](size_t worker, std::string_view user, int64_t ts, std::string_view product, uint32_t price) {
                    const auto category = price ? inputs->product_category(product) : 0;
                    auto& rows = pending[worker];
                    rows.names[rows.count] = user;
                    rows.events[rows.count++] = {ts, 0, category, price};
                    if (rows.count == PendingEvents::capacity) rows.flush(*inputs, workers[worker]);
                });
            for (size_t i = 0; i < threads; ++i) pending[i].flush(*inputs, workers[i]);
        }
        checkpoint("events");
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
