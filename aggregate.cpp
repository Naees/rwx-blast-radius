#include "aggregate.hpp"

#include <algorithm>
#include <atomic>
#include <limits>
#include <memory>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <utility>

namespace shop {
namespace {

constexpr int64_t day_ms = 86400000;
constexpr int64_t week_ms = 7 * day_ms;
constexpr int64_t session_gap_ms = 1800000;
constexpr uint32_t no_category = UINT32_MAX;

int32_t utc_day(int64_t ms) {
    return static_cast<int32_t>(ms / day_ms - (ms % day_ms < 0));
}

template<class F>
void parallel(size_t count, unsigned threads, F fn) {
    if (!count) return;
    threads = static_cast<unsigned>(std::min<size_t>(threads, count));
    std::atomic<size_t> next{0};
    auto run = [&] {
        for (size_t i; (i = next.fetch_add(1, std::memory_order_relaxed)) < count;)
            fn(i);
    };
    std::vector<std::thread> workers;
    for (unsigned i = 1; i < threads; ++i) workers.emplace_back(run);
    run();
    for (auto& worker : workers) worker.join();
}

struct TimeEvent { int64_t ts; uint32_t category, price; };
struct TimeClick { int64_t ts; uint32_t campaign, index; };
struct ClickIndex { uint32_t person, index; };

struct ClickTiles {
    std::unique_ptr<ClickIndex[]> entries;
    std::vector<uint32_t> offsets;
};

// Turn per-person row counts into write positions within one tile.
uint32_t prefix(std::vector<uint32_t>& positions) {
    uint32_t start = 0;
    for (auto& pos : positions) {
        uint32_t end = start + pos;
        pos = start;
        start = end;
    }
    return start;
}

constexpr uint32_t tile_size = 1u << WorkerEvents::tile_bits;
ClickTiles tile_clicks(const std::vector<Click>& clicks, size_t tiles, unsigned threads) {
    if (clicks.size() > UINT32_MAX) throw std::length_error("too many clicks");
    ClickTiles result;
    result.entries.reset(new ClickIndex[clicks.size()]);
    result.offsets.resize(tiles + 1);
    std::vector<std::vector<uint32_t>> counts(threads, std::vector<uint32_t>(tiles));
    parallel(threads, threads, [&](size_t t) {
        auto& count = counts[t];
        const size_t begin = clicks.size() * t / threads;
        const size_t end = clicks.size() * (t + 1) / threads;
        for (size_t i = begin; i < end; ++i)
            ++count[clicks[i].person >> WorkerEvents::tile_bits];
    });
    uint32_t n = 0;
    for (size_t tile = 0; tile < tiles; ++tile) {
        result.offsets[tile] = n;
        for (auto& part : counts) {
            uint32_t end = n + part[tile];
            part[tile] = n;
            n = end;
        }
    }
    result.offsets[tiles] = n;
    parallel(threads, threads, [&](size_t t) {
        auto& next = counts[t];
        const size_t begin = clicks.size() * t / threads;
        const size_t end = clicks.size() * (t + 1) / threads;
        for (size_t i = begin; i < end; ++i) {
            uint32_t person = clicks[i].person;
            result.entries[next[person >> WorkerEvents::tile_bits]++] = {person, static_cast<uint32_t>(i)};
        }
    });
    return result;
}

struct CampaignTotals {
    uint32_t sessions = 0, purchasing = 0, assists = 0;
    int64_t revenue = 0;
};

using Days = std::unordered_map<int32_t, std::vector<CampaignTotals>>;

struct Session {
    int64_t start;
    uint32_t day_index;
};

struct SessionDay {
    CampaignTotals* campaigns;
    uint32_t end_session; // Exclusive position in this person's sorted sessions.
};

struct Worker {
    Days totals;
    std::vector<UserDay> users;
    std::vector<Session> sessions;
    std::vector<SessionDay> days;
    std::vector<int64_t> category_revenue;
    std::vector<uint32_t> used_categories;
    std::vector<uint32_t> covered_until, coverage_person;

    Worker(size_t categories, size_t campaigns)
        : category_revenue(categories), covered_until(campaigns),
          coverage_person(campaigns, UINT32_MAX) {}
};

void process_person(const Inputs& input, uint32_t person, TimeEvent* events, uint32_t ne,
                    TimeClick* clicks, uint32_t nc, const std::vector<Click>& all_clicks, Worker& w) {
    std::sort(events, events + ne, [](const auto& a, const auto& b) { return a.ts < b.ts; });
    if (nc > 1) {
        // The last eligible click becomes the winner. At equal times the smallest ID wins.
        std::sort(clicks, clicks + nc, [&](const auto& a, const auto& b) {
            return a.ts < b.ts || (a.ts == b.ts && all_clicks[a.index].id > all_clicks[b.index].id);
        });
    }

    w.sessions.clear();
    w.days.clear();
    uint32_t click_end = 0;
    int32_t current_day = std::numeric_limits<int32_t>::min();
    for (uint32_t i = 0; i < ne;) {
        uint32_t begin = i;
        int64_t start = events[i].ts;
        int32_t day = utc_day(start);
        if (current_day != day) {
            for (auto cat : w.used_categories) w.category_revenue[cat] = 0;
            w.used_categories.clear();
            auto [it, inserted] = w.totals.try_emplace(day);
            if (inserted) it->second.resize(input.campaigns.size());
            w.days.push_back({it->second.data(), 0});
            w.users.push_back({person, day, 0, 0, 0, no_category});
            current_day = day;
        }
        auto& user = w.users.back();
        int64_t revenue = 0;
        do {
            const auto& e = events[i++];
            if (e.price == 0) continue;
            auto& sum = w.category_revenue[e.category];
            if (sum == 0) w.used_categories.push_back(e.category);
            sum += e.price;
            revenue += e.price;
            if (user.category == no_category || sum > w.category_revenue[user.category] ||
                (sum == w.category_revenue[user.category] && e.category != user.category &&
                 input.categories[e.category] < input.categories[user.category])) {
                user.category = e.category;
            }
        } while (i < ne && events[i].ts - events[i - 1].ts <= session_gap_ms);
        user.events += i - begin;
        ++user.sessions;
        user.revenue += revenue;
        while (click_end < nc && clicks[click_end].ts <= start) ++click_end;
        uint32_t winner = 0;
        if (click_end && clicks[click_end - 1].ts >= start - week_ms)
            winner = clicks[click_end - 1].campaign;
        auto& totals = w.days.back().campaigns[winner];
        ++totals.sessions;
        totals.purchasing += (revenue != 0);
        totals.revenue += revenue;
        if (nc) {
            w.sessions.push_back({start, static_cast<uint32_t>(w.days.size() - 1)});
            w.days.back().end_session = static_cast<uint32_t>(w.sessions.size());
        }
    }

    // A click covers a contiguous interval in the sorted session-start array.
    // The ends of those intervals only advance, even when a campaign repeats.
    // Count the newly covered suffix in day-size pieces, eliminating duplicate assists.
    uint32_t lo = 0, hi = 0, ns = static_cast<uint32_t>(w.sessions.size());
    for (uint32_t i = 0; i < nc; ++i) {
        const auto& click = clicks[i];
        while (lo < ns && w.sessions[lo].start < click.ts) ++lo;
        if (lo == ns) break;
        hi = std::max(hi, lo);
        while (hi < ns && w.sessions[hi].start <= click.ts + week_ms) ++hi;
        auto c = click.campaign;
        uint32_t j = lo;
        if (w.coverage_person[c] == person) j = std::max(j, w.covered_until[c]);
        w.coverage_person[c] = person;
        w.covered_until[c] = hi;
        while (j < hi) {
            const auto& day = w.days[w.sessions[j].day_index];
            uint32_t end = std::min(hi, day.end_session);
            day.campaigns[c].assists += end - j;
            j = end;
        }
    }
}

} // namespace

DailyTables aggregate(const Inputs& inputs, std::vector<WorkerEvents>& events,
                      std::vector<Click>& clicks, unsigned threads) {
    threads = std::max(threads, 1u);
    uint32_t people = inputs.person_count();
    // Process each range of person IDs independently. Both regrouped arrays are
    // local to the worker and are reused for its next range.
    const size_t ntiles = (size_t(people) + tile_size - 1) >> WorkerEvents::tile_bits;
    auto click_tiles = tile_clicks(clicks, ntiles, threads);
    std::vector<Worker> workers;
    for (unsigned i = 0; i < threads; ++i)
        workers.emplace_back(inputs.categories.size(), inputs.campaigns.size());
    std::atomic<size_t> next_tile{0};
    parallel(threads, threads, [&](size_t thread) {
        auto& worker = workers[thread];
        std::vector<TimeEvent> event_rows;
        std::vector<TimeClick> click_rows;
        std::vector<uint32_t> event_ends, click_ends;
        for (;;) {
            size_t tile = next_tile.fetch_add(1, std::memory_order_relaxed);
            if (tile >= ntiles) break;
            uint32_t base = uint32_t(tile << WorkerEvents::tile_bits);
            uint32_t count = std::min(tile_size, people - base);
            event_ends.assign(count, 0);
            click_ends.assign(count, 0);
            for (auto& w : events) {
                if (tile < w.tiles.size())
                    for (auto& e : w.tiles[tile].events) ++event_ends[e.person - base];
            }
            uint32_t click_begin = click_tiles.offsets[tile], click_end = click_tiles.offsets[tile + 1];
            for (uint32_t i = click_begin; i < click_end; ++i)
                ++click_ends[click_tiles.entries[i].person - base];
            event_rows.resize(prefix(event_ends));
            click_rows.resize(prefix(click_ends));
            for (auto& w : events) {
                if (tile >= w.tiles.size()) continue;
                auto& source = w.tiles[tile];
                for (const auto& event : source.events) {
                    TimeEvent row{inputs.window_start + event.offset, 0, 0};
                    if (event.detail) {
                        const auto& detail = source.details[event.detail - 1];
                        row = {detail.ts, detail.category, detail.price};
                    }
                    event_rows[event_ends[event.person - base]++] = row;
                }
                source = {};
            }
            for (uint32_t i = click_begin; i < click_end; ++i) {
                auto entry = click_tiles.entries[i];
                auto& c = clicks[entry.index];
                click_rows[click_ends[entry.person - base]++] = {c.ts, c.campaign, entry.index};
            }
            uint32_t e = 0, c = 0;
            for (uint32_t j = 0; j < count; ++j) {
                uint32_t ne = event_ends[j] - e, nc = click_ends[j] - c;
                if (ne) {
                    process_person(inputs, base + j, event_rows.data() + e, ne,
                                   nc ? click_rows.data() + c : nullptr, nc, clicks, worker);
                }
                e += ne;
                c += nc;
            }
        }
    });
    std::vector<Click>().swap(clicks);
    DailyTables result;
    result.users.reserve(workers.size());
    Days totals;
    for (auto& worker : workers) {
        result.users.push_back(std::move(worker.users));
        for (const auto& [day, campaigns] : worker.totals) {
            auto [it, fresh] = totals.try_emplace(day);
            if (fresh) it->second.resize(campaigns.size());
            for (size_t c = 0; c < campaigns.size(); ++c) {
                auto& acc = it->second[c];
                acc.sessions += campaigns[c].sessions;
                acc.purchasing += campaigns[c].purchasing;
                acc.assists += campaigns[c].assists;
                acc.revenue += campaigns[c].revenue;
            }
        }
    }
    for (const auto& [day, campaigns] : totals) {
        for (uint32_t c = 0; c < campaigns.size(); ++c) {
            const auto& t = campaigns[c];
            if (t.sessions || t.assists)
                result.campaigns.push_back({c, day, t.sessions, t.purchasing, t.assists, t.revenue});
        }
    }
    return result;
}

} // namespace shop
