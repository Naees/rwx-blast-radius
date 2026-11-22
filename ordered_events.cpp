#include "events.hpp"
#include "csv.hpp"
#include "event_jobs.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <future>
#include <stdexcept>
#include <system_error>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace shop {
namespace {

struct MappedFile {
    char* data = nullptr;
    std::size_t size = 0;

    explicit MappedFile(const std::filesystem::path& path) {
        const int fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) throw std::system_error(errno, std::generic_category(), path.string());
        struct stat statbuf{};
        if (::fstat(fd, &statbuf) != 0) {
            int err = errno;
            ::close(fd);
            throw std::system_error(err, std::generic_category(), path.string());
        }
        size = static_cast<std::size_t>(statbuf.st_size);
        if (size) {
            data = static_cast<char*>(::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0));
            if (data == MAP_FAILED) {
                int err = errno;
                ::close(fd);
                throw std::system_error(err, std::generic_category(), path.string());
            }
        }
        ::close(fd);
    }
    MappedFile(const MappedFile&) = delete;
    MappedFile(MappedFile&& other) noexcept : data(other.data), size(other.size) { other.data = nullptr; }
    ~MappedFile() { if (data) ::munmap(data, size); }
};

const char* end_of_line(const char* p, const char* end) {
    const auto* found = static_cast<const char*>(std::memchr(p, '\n', end - p));
    return found ? found : end;
}

inline uint64_t mix(uint64_t a, uint64_t b) {
    const auto prod = static_cast<__uint128_t>(a) * b;
    return static_cast<uint64_t>(prod) ^ static_cast<uint64_t>(prod >> 64);
}

template <typename T>
inline T load(const char* p) {
    T x;
    std::memcpy(&x, p, sizeof(x));
    return x;
}

uint64_t key_hash(std::string_view s) {
    constexpr uint64_t a = 0xa0761d6478bd642fULL, b = 0xe7037ed1a0b428dbULL;
    uint64_t seed = s.size() ^ a;
    const char* p = s.data();
    std::size_t n = s.size();
    while (n > 16) {
        seed = mix(load<uint64_t>(p) ^ seed, load<uint64_t>(p + 8) ^ b);
        n -= 16;
        p += 16;
    }
    uint64_t first = 0, last = 0;
    if (n >= 8) {
        first = load<uint64_t>(p);
        last = load<uint64_t>(p + n - 8);
    } else if (n >= 4) {
        first = load<uint32_t>(p);
        last = load<uint32_t>(p + n - 4);
    } else if (n) {
        first = (uint64_t(static_cast<uint8_t>(p[0])) << 16) |
                (uint64_t(static_cast<uint8_t>(p[n / 2])) << 8) | static_cast<uint8_t>(p[n - 1]);
    }
    return mix(first ^ b, last ^ seed);
}

// Unwrap the ID here. Quoting errors anywhere in the record are checked by parse_csv.
std::string_view row_id(const char* p, std::size_t len) {
    const char* comma = static_cast<const char*>(std::memchr(p, ',', len));
    if (!comma) return {};
    const char* stop = comma;
    if (stop - p >= 2 && *p == '"' && stop[-1] == '"') { ++p; --stop; }
    return {p, static_cast<std::size_t>(stop - p)};
}

struct Row {
    uint32_t offset;
    uint32_t length;
};

struct Chunk {
    const char* data;
    const char* end;
    const char* file_start;
    const char* file_end;
    std::vector<Row> rows;

    std::string_view line(const Row& row) const {
        const char* p = data + row.offset;
        if (row.length != UINT32_MAX) return {p, row.length};
        // Only a chunk's final row can extend beyond its nominal 1 MiB.
        return {p, static_cast<std::size_t>(end_of_line(p, file_end) - p)};
    }
};

struct CandidateKeys {
    struct Packed {
        uint32_t hash, location;
    };
    struct Wide {
        uint32_t hash, chunk, row;
    };
    std::vector<Packed> packed;
    std::vector<Wide> wide;

    void add(uint32_t hash, uint32_t chunk, uint32_t row) {
        if (chunk <= UINT16_MAX && row <= UINT16_MAX) {
            packed.push_back({hash, (chunk << 16) | row});
        } else {
            wide.push_back({hash, chunk, row});
        }
    }

    template<class Function>
    void each(Function visit) const {
        for (const auto& item : packed)
            visit(item.hash, item.location >> 16, item.location & UINT16_MAX);
        for (const auto& item : wide)
            visit(item.hash, item.chunk, item.row);
    }
};

struct ScreenWord {
    uint64_t seen = 0, repeated = 0;
};

struct IngestDates {
    struct Day {
        char prefix[10]{};
        int64_t start_ms = 0;
    };
    std::array<Day, 32> dates{};

    bool on_time(int64_t event_ms, std::string_view ingest) {
        // ingest_ts is guaranteed valid UTC F1. When its midnight is at or before the
        // event, every time on that date passes the 24-hour limit.
        constexpr int64_t day_ms = 86400000;
        if (ingest.size() != 20) return false;
        const char* p = ingest.data();
        const unsigned slot = (static_cast<unsigned char>(p[8]) * 10 + static_cast<unsigned char>(p[9])) & 31;
        auto& date = dates[slot];
        if (std::memcmp(date.prefix, p, sizeof(date.prefix)) != 0) {
            const auto full_ms = parse_timestamp(ingest);
            if (full_ms == invalid_timestamp) return false;
            int64_t days = full_ms / day_ms - (full_ms % day_ms < 0);
            date.start_ms = days * day_ms;
            std::memcpy(date.prefix, p, sizeof(date.prefix));
        }
        if (date.start_ms <= event_ms) return true;
        const int64_t elapsed = date.start_ms - event_ms;
        if (elapsed > day_ms) return false;
        const unsigned sec = digit_pair(p + 11) * 3600 + digit_pair(p + 14) * 60 + digit_pair(p + 17);
        return elapsed + sec * 1000 <= day_ms;
    }
};

template<class Consumer>
void validate_event(std::size_t worker, IngestDates& dates, std::string_view line, int64_t start_ms, int64_t end_ms,
                    const Consumer& consume) {
    std::array<std::string_view, 7> f;
    if (!parse_csv(line, f) || f[1].empty() || f[5].empty()) return;
    const auto ts = parse_timestamp(f[2]);
    if (ts < start_ms || ts >= end_ms || ts == invalid_timestamp) return;
    if (!dates.on_time(ts, f[3])) return;
    uint32_t cents = 0;
    if (f[4] == "purchase") {
        cents = parse_price(f[6]);
        if (!cents) return;
    } else if (f[4] != "cart" && f[4] != "view") return;
    consume(worker, f[1], ts, f[5], cents);
}

class OrderedEvents {
public:
    OrderedEvents(const std::filesystem::path& data_dir, size_t workers);
    template<class Consumer>
    void consume(int64_t start_ms, int64_t end_ms, const Consumer& consumer) const;
    size_t row_count() const;

private:
    std::vector<MappedFile> files;
    std::vector<Chunk> chunks;
    size_t nthreads;
};

OrderedEvents::OrderedEvents(const std::filesystem::path& data_dir, size_t workers)
    : nthreads(std::max<size_t>(1, workers)) {
    std::size_t total_bytes = 0;
    constexpr std::size_t chunk_size = 1 << 20;
    for (const auto& entry : std::filesystem::directory_iterator(data_dir / "events")) {
        if (entry.path().extension() != ".csv") continue;
        files.emplace_back(entry.path());
        auto& file = files.back();
        if (!file.size) continue;
        total_bytes += file.size;
        const char* end = file.data + file.size;
        for (const char* p = file.data; p < end;) {
            const char* next = p + std::min<std::size_t>(end - p, chunk_size);
            chunks.push_back({p, next, file.data, end, {}});
            p = next;
        }
    }
    if (chunks.empty()) return;
    nthreads = std::min(nthreads, chunks.size());
    std::size_t bucket_count = 1;
    while (bucket_count < 1024 && bucket_count * (2U << 20) < total_bytes) bucket_count *= 2;
    using Buckets = std::vector<CandidateKeys>;
    std::vector<Buckets> keys(nthreads, Buckets(bucket_count));
    run_jobs(nthreads, chunks.size(), [&](std::size_t worker, std::size_t i) {
        auto& chunk = chunks[i];
        chunk.rows.reserve(static_cast<std::size_t>(chunk.end - chunk.data) / 78 + 16);
        const char* p = chunk.data;
        // Each row belongs to the chunk containing its first byte. Skip the file header
        // or a partial row at the front without touching the next chunk's pages.
        if (p == chunk.file_start || p[-1] != '\n') {
            const char* stop = end_of_line(p, chunk.end);
            if (stop == chunk.end) return;
            p = stop + 1;
        }
        while (p < chunk.end) {
            const char* stop = end_of_line(p, chunk.file_end);
            const auto len = static_cast<std::size_t>(stop - p);
            const auto id = row_id(p, len);
            if (!id.empty()) {
                const auto hash = key_hash(id);
                auto& bucket = keys[worker][hash & (bucket_count - 1)];
                bucket.add(static_cast<uint32_t>(hash >> 32), static_cast<uint32_t>(i),
                           static_cast<uint32_t>(chunk.rows.size()));
                chunk.rows.push_back({static_cast<uint32_t>(p - chunk.data),
                                      static_cast<uint32_t>(std::min<std::size_t>(len, UINT32_MAX))});
            }
            p = stop < chunk.file_end ? stop + 1 : stop;
        }
    });

    // Gather repeated fingerprints separately. Most rows need no exact ID comparison.
    // Delay fetching their CSV bytes until every candidate can be read by file offset.
    constexpr uint32_t none = UINT32_MAX;
    struct Hit {
        uint32_t chunk, row, group;
        uint32_t next = UINT32_MAX;
        int64_t ingest = 0;
        std::string id;
        bool keep = false;
    };
    struct Collisions {
        std::vector<Hit> hits;
        uint32_t groups = 0;
    };
    std::vector<Collisions> collisions(bucket_count);
    run_jobs(nthreads, bucket_count, [&](std::size_t, std::size_t bucket) {
        size_t count = 0;
        for (const auto& worker : keys) count += worker[bucket].packed.size() + worker[bucket].wide.size();
        if (count < 2) return;

        // Count every row whose bin is occupied at least twice. Unique bins
        // cannot contain duplicate IDs. Colliding bins still compare full hashes below.
        size_t word_count = 1;
        while (word_count < (count + 7) / 8) word_count *= 2;
        std::vector<ScreenWord> screen(word_count);
        size_t candidates = 0;
        for (const auto& worker : keys) {
            worker[bucket].each([&](uint32_t hash, uint32_t, uint32_t) {
                auto& word = screen[(hash >> 6) & (word_count - 1)];
                const uint64_t bit = uint64_t{1} << (hash & 63);
                const uint64_t previous = word.seen & bit;
                candidates += (previous != 0) + ((previous & ~word.repeated) != 0);
                word.repeated |= previous;
                word.seen |= bit;
            });
        }
        if (!candidates) return;
        size_t capacity = 4;
        while (capacity < candidates + candidates / 3) capacity *= 2;
        struct Slot {
            uint32_t hash = 0, chunk = 0, row = UINT32_MAX, group = UINT32_MAX;
        };
        std::vector<Slot> slots(capacity);
        auto& out = collisions[bucket];
        auto add = [&](uint32_t chunk, uint32_t row, uint32_t group) {
            out.hits.emplace_back();
            auto& hit = out.hits.back();
            hit.chunk = chunk;
            hit.row = row;
            hit.group = group;
        };
        for (const auto& worker : keys) {
            worker[bucket].each([&](uint32_t hash, uint32_t chunk, uint32_t row) {
                const auto& word = screen[(hash >> 6) & (word_count - 1)];
                if (!(word.repeated & (uint64_t{1} << (hash & 63)))) return;
                size_t slot = hash & (capacity - 1);
                while (slots[slot].row != none && slots[slot].hash != hash)
                    slot = (slot + 1) & (capacity - 1);
                auto& entry = slots[slot];
                if (entry.row == none) {
                    entry = {hash, chunk, row, none};
                    return;
                }
                if (entry.group == none) {
                    entry.group = out.groups++;
                    add(entry.chunk, entry.row, entry.group);
                }
                add(chunk, row, entry.group);
            });
        }
    });
    keys.clear();

    std::vector<std::vector<Hit*>> reads(chunks.size());
    for (auto& out : collisions)
        for (auto& hit : out.hits) reads[hit.chunk].push_back(&hit);
    run_jobs(nthreads, chunks.size(), [&](std::size_t, std::size_t i) {
        auto& chunk = chunks[i];
        const auto& records = reads[i];
        if (records.empty()) return;
        // Rows already have dense per-chunk ordinals. This staging array orders
        // the small suspect set without chasing pointers in a comparison sort.
        std::vector<Hit*> ordered(chunk.rows.size(), nullptr);
        for (Hit* hit : records) ordered[hit->row] = hit;
        std::array<std::string_view, 7> fields;
        for (auto* hit : ordered) {
            if (!hit || !parse_csv(chunk.line(chunk.rows[hit->row]), fields)) continue;
            hit->id.assign(fields[0]);
            hit->ingest = parse_timestamp(fields[3]);
        }
    });

    run_jobs(nthreads, collisions.size(), [&](std::size_t, std::size_t b) {
        auto& out = collisions[b];
        std::vector<uint32_t> heads(out.groups, none);
        for (uint32_t j = 0; j < out.hits.size(); ++j) {
            auto& hit = out.hits[j];
            if (hit.id.empty()) continue;
            auto* head = &heads[hit.group];
            while (*head != none && out.hits[*head].id != hit.id) head = &out.hits[*head].next;
            if (*head == none) {
                *head = j;
                hit.keep = true;
            } else if (hit.ingest < out.hits[*head].ingest) {
                auto& old = out.hits[*head];
                old.keep = false;
                hit.keep = true;
                hit.next = old.next;
                *head = j;
            }
        }
    });
    run_jobs(nthreads, chunks.size(), [&](std::size_t, std::size_t i) {
        for (const auto* hit : reads[i]) if (!hit->keep) chunks[i].rows[hit->row].offset = UINT32_MAX;
    });
}

template<class Consumer>
void OrderedEvents::consume(int64_t start_ms, int64_t end_ms, const Consumer& consume) const {
    run_jobs(nthreads, chunks.size(), [&](std::size_t worker, std::size_t i) {
        const auto& chunk = chunks[i];
        IngestDates dates;
        for (const auto& row : chunk.rows) {
            if (row.offset != UINT32_MAX) validate_event(worker, dates, chunk.line(row), start_ms, end_ms, consume);
        }
    });
}

std::size_t OrderedEvents::row_count() const {
    std::size_t count = 0;
    for (const auto& chunk : chunks) count += chunk.rows.size();
    return count;
}

struct alignas(128) PendingEvents {
    static constexpr size_t capacity = Inputs::lookup_batch_size;
    std::array<std::string_view, capacity> names;
    std::array<Event, capacity> events;
    size_t count = 0;
    void flush(const Inputs& inputs, WorkerEvents& worker) {
        std::array<uint32_t, capacity> resolved;
        inputs.resolve_people(names.data(), resolved.data(), count);
        for (size_t i = 0; i < count; ++i) {
            events[i].person = resolved[i];
            worker.add(inputs.window_start, events[i]);
        }
        count = 0;
    }
};

} // namespace

std::unique_ptr<Inputs> read_ordered_events(const std::string& data, std::vector<WorkerEvents>& workers) {
    // Indexing event IDs is independent of loading catalogs, identity links and clicks.
    auto loading = std::async(std::launch::async, [&] { return std::make_unique<Inputs>(data); });
    OrderedEvents batch(data, workers.size());
    auto inputs = loading.get();
    const size_t per_worker = batch.row_count() / workers.size() + 1;
    for (auto& worker : workers) worker.reserve(inputs->person_count(), per_worker);
    std::vector<PendingEvents> pending(workers.size());
    batch.consume(inputs->window_start, inputs->window_end,
        [&](size_t worker, std::string_view user, int64_t ts, std::string_view product, uint32_t price) {
            auto& rows = pending[worker];
            const auto category = price ? inputs->product_category(product) : 0;
            rows.names[rows.count] = user;
            rows.events[rows.count++] = {ts, 0, category, price};
            if (rows.count == PendingEvents::capacity) rows.flush(*inputs, workers[worker]);
        });
    for (size_t i = 0; i < workers.size(); ++i) pending[i].flush(*inputs, workers[i]);
    return inputs;
}

} // namespace shop
