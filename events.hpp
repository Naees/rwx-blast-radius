#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <string_view>

namespace shop {

using EventConsumer = std::function<void(std::size_t worker, std::string_view user, int64_t ts_ms,
                                         std::string_view product, uint32_t purchase_cents)>;

class EventBatch {
public:
    EventBatch(const std::filesystem::path& data_dir, std::size_t nthreads);
    ~EventBatch();
    // Candidate rows before duplicate removal and validation.
    std::size_t row_count() const;

    // Each worker index belongs to one thread. Input views live until the batch is destroyed.
    void consume(int64_t start_ms, int64_t end_ms, const EventConsumer& consumer) const;

private:
    struct Storage;
    std::unique_ptr<Storage> storage_;
};

} // namespace shop
