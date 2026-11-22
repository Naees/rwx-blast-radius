#pragma once

#include <algorithm>
#include <atomic>
#include <cstddef>
#include <exception>
#include <mutex>
#include <thread>
#include <vector>

namespace shop {

template<class Function>
void run_jobs(size_t threads, size_t count, Function function) {
    std::atomic<size_t> next{0};
    std::exception_ptr failure;
    std::mutex mutex;
    auto body = [&](size_t worker) {
        try {
            for (size_t i; (i = next.fetch_add(1, std::memory_order_relaxed)) < count;)
                function(worker, i);
        } catch (...) {
            next.store(count, std::memory_order_relaxed);
            std::lock_guard<std::mutex> guard(mutex);
            if (!failure) failure = std::current_exception();
        }
    };
    std::vector<std::thread> pool;
    for (size_t i = 1; i < std::min(count, threads); ++i) pool.emplace_back(body, i);
    body(0);
    for (auto& t : pool) t.join();
    if (failure) std::rethrow_exception(failure);
}

} // namespace shop
