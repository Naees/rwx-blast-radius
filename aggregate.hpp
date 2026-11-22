#pragma once

#include "inputs.hpp"
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace shop {

struct Event {
    int64_t ts;
    uint32_t person, category, price;
};

struct alignas(128) WorkerEvents {
    static constexpr unsigned tile_bits = 12;
    // Most rows only need a person and millisecond offset. Purchases and offsets
    // wider than 32 bits store their full payload in the same tile.
    struct Compact { uint32_t person, offset, detail; };
    struct Detail { int64_t ts; uint32_t category, price; };
    struct Tile { std::vector<Compact> events; std::vector<Detail> details; };
    std::vector<Tile> tiles;

    void reserve(uint32_t people, size_t expected_events) {
        size_t count = (size_t(people) + (1u << tile_bits) - 1) >> tile_bits;
        if (!count) return;
        tiles.resize(count);
        size_t estimate = std::min<size_t>(expected_events / count, 65536);
        for (auto& tile : tiles) tile.events.reserve(estimate);
    }

    void add(int64_t window_start, Event event) {
        size_t index = event.person >> tile_bits;
        if (index >= tiles.size()) tiles.resize(index + 1);
        auto& tile = tiles[index];
        auto offset = static_cast<uint64_t>(event.ts - window_start);
        uint32_t detail = 0;
        if (event.price || offset > UINT32_MAX) {
            tile.details.push_back({event.ts, event.category, event.price});
            detail = static_cast<uint32_t>(tile.details.size());
        }
        tile.events.push_back({event.person, static_cast<uint32_t>(offset), detail});
    }
};

struct UserDay {
    uint32_t person;
    int32_t day;
    uint32_t sessions, events;
    int64_t revenue;
    uint32_t category; // UINT32_MAX when no purchases.
};

struct CampaignDay {
    uint32_t campaign;
    int32_t day;
    uint32_t sessions, purchasing_sessions, assisted_sessions;
    int64_t revenue;
};

struct DailyTables {
    std::vector<std::vector<UserDay>> users;
    std::vector<CampaignDay> campaigns;
};

DailyTables aggregate(const Inputs& inputs, std::vector<WorkerEvents>& events,
                      std::vector<Click>& clicks, unsigned threads);

} // namespace shop
