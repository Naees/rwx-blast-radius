#include "events.hpp"
#include "csv.hpp"
#include "event_jobs.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <system_error>
#include <unistd.h>

namespace shop {
namespace {

constexpr uint32_t absent = UINT32_MAX;
constexpr uint32_t detail_mask = (1u << 28) - 1;
constexpr size_t block_rows = 1024;
constexpr size_t input_chunk = 1 << 20;

template<class T>
T load(const char* p) {
    T result;
    std::memcpy(&result, p, sizeof(result));
    return result;
}

uint64_t mix(uint64_t key) {
    key ^= key >> 30;
    key *= 0xbf58476d1ce4e5b9ULL;
    key ^= key >> 27;
    key *= 0x94d049bb133111ebULL;
    return key ^ (key >> 31);
}

struct Id {
    uint64_t word, hash;
    uint32_t mode;
};

uint64_t inline_hash(uint64_t word, uint32_t mode) {
    return mix(word ^ (0x9e3779b97f4a7c15ULL * mode));
}

// The mode preserves the original length, including trailing NUL bytes. A zero mode
// stores the entire string in the partition. Equality is lossless in every mode.
Id identify(std::string_view id) {
    uint64_t word = 0;
    uint32_t mode = 0;
    if (id.size() <= 8) {
        if (id.size() == 8) word = load<uint64_t>(id.data());
        else std::memcpy(&word, id.data(), id.size());
        mode = static_cast<uint32_t>(id.size());
    } else if (id.size() == 9) {
        word = load<uint64_t>(id.data());
        uint64_t last = static_cast<uint8_t>(id[8]);
        if (!(word & 0x8080808080808080ULL) && last < 128) {
            // Scatter the ninth ASCII byte's seven bits into the eight preceding bytes' spare bits.
            constexpr uint64_t copies = (1ULL << 7) | (1ULL << 14) | (1ULL << 21) | (1ULL << 28) |
                                        (1ULL << 35) | (1ULL << 42) | (1ULL << 49);
            word |= (last * copies) & 0x0080808080808080ULL;
            mode = 9;
        }
    }
    uint64_t hash = mode ? inline_hash(word, mode) : std::hash<std::string_view>{}(id);
    return {word, hash, mode};
}

struct Candidate {
    uint64_t key;
    uint32_t ingest, person, offset, meta;

    uint32_t id_mode() const { return meta >> 28; }
    uint32_t detail_index() const { return meta & detail_mask; }
};
static_assert(sizeof(Candidate) == 24);
struct Detail {
    int64_t ingest, ts;
    uint32_t category, price;
};

struct Partition {
    size_t count = 0;
    std::vector<std::unique_ptr<Candidate[]>> blocks;
    std::vector<char> strings;
    std::vector<Detail> details;

    Candidate& push(Candidate row) {
        if (count % block_rows == 0) blocks.emplace_back(new Candidate[block_rows]);
        Candidate& saved = blocks.back()[count % block_rows];
        saved = row;
        ++count;
        return saved;
    }
    template<class Function>
    void each(Function visit) {
        size_t left = count;
        for (auto& block : blocks) {
            size_t size = std::min(left, block_rows);
            for (size_t i = 0; i < size; ++i) visit(block[i]);
            left -= size;
        }
    }
    uint64_t save_string(std::string_view id, uint32_t hash) {
        // An arena entry contains a length, the low hash bits, and the original bytes.
        const size_t base = strings.size();
        strings.resize(base + 12 + id.size());
        char* p = strings.data() + base;
        uint64_t len = id.size();
        std::memcpy(p, &len, 8);
        std::memcpy(p + 8, &hash, 4);
        std::memcpy(p + 12, id.data(), id.size());
        return base;
    }
    std::string_view string(uint64_t offset) const {
        const char* p = strings.data() + offset;
        return {p + 12, static_cast<size_t>(load<uint64_t>(p))};
    }
    uint32_t slot_hash(const Candidate& r) const {
        if (r.id_mode()) return static_cast<uint32_t>(inline_hash(r.key, r.id_mode()));
        return load<uint32_t>(strings.data() + r.key + 8);
    }
    const Detail& detail(const Candidate& r) const { return details[r.detail_index() - 1]; }
};

struct File {
    int fd;
    size_t bytes;
    explicit File(const std::filesystem::path& p) : fd(-1), bytes(std::filesystem::file_size(p)) {
        fd = ::open(p.c_str(), O_RDONLY);
        if (fd < 0) throw std::system_error(errno, std::generic_category(), p.string());
    }
    File(File&& other) noexcept : fd(other.fd), bytes(other.bytes) { other.fd = -1; }
    ~File() { if (fd >= 0) ::close(fd); }
};

void read_bytes(const File& file, size_t at, size_t n, char* dest) {
    while (n) {
        auto got = ::pread(file.fd, dest, n, static_cast<off_t>(at));
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) throw std::system_error(errno, std::generic_category(), "event pread");
        if (!got) throw std::runtime_error("events file shortened during scan");
        n -= got;
        at += got;
        dest += got;
    }
}

// Scan a complete sequence of newline-terminated records in 64-byte groups.
// Quote totals include every byte, so a quote inside an otherwise unused field
// still invalidates the row. Only matched quotes on field boundaries are removed.
template<class Function>
void parse_records(const char* bytes, size_t size, Function process) {
    std::array<std::string_view, 7> fields;
    const char* field = bytes;
    size_t count = 0, quote_count = 0, paired_quotes = 0;
    for (size_t offset = 0; offset < size; offset += 64) {
        size_t width = std::min<size_t>(64, size - offset);
        const char* block = bytes + offset;
        uint64_t endings = 0, quotes = 0, lines = 0;
        size_t i = 0;
#if defined(__AVX2__) || defined(__SSE2__) || defined(__aarch64__)
        for (; i + detail::vector_bytes <= width; i += detail::vector_bytes) {
            uint32_t comma, quote;
            detail::csv_masks(block + i, comma, quote);
            auto newline = detail::delimiter_mask(block + i, '\n');
            endings |= uint64_t(comma | newline) << i;
            quotes |= uint64_t(quote) << i;
            lines |= uint64_t(newline) << i;
        }
#endif
        for (; i < width; ++i) {
            const uint64_t bit = uint64_t{1} << i;
            if (block[i] == ',' || block[i] == '\n') endings |= bit;
            if (block[i] == '"') quotes |= bit;
            if (block[i] == '\n') lines |= bit;
        }
        while (endings) {
            const uint64_t bit = endings & -endings;
            const char* end = block + __builtin_ctzll(endings);
            if (count < fields.size()) {
                const bool wrapped = end - field >= 2 && field[0] == '"' && end[-1] == '"';
                fields[count] = {field + wrapped, static_cast<size_t>(end - field - 2 * wrapped)};
                paired_quotes += 2 * wrapped;
            }
            ++count;
            if (lines & bit) {
                quote_count += __builtin_popcountll(quotes & (bit - 1));
                quotes &= ~(bit - 1);
                if (count == fields.size() && quote_count == paired_quotes) process(fields);
                count = quote_count = paired_quotes = 0;
            }
            field = end + 1;
            endings &= endings - 1;
        }
        quote_count += __builtin_popcountll(quotes);
    }
}

// A row belongs to the chunk containing its first byte. Extend the last row
// before parsing so every field view remains stable until the chunk is consumed.
template<class Function>
void read_chunk(const File& file, size_t start, std::vector<char>& buffer, Function process) {
    const size_t base = start ? start - 1 : 0;
    const size_t logical_end = std::min(file.bytes, start + input_chunk) - base;
    size_t end = std::min(file.bytes - base, logical_end + 512);
    buffer.resize(end);
    read_bytes(file, base, end, buffer.data());
    size_t pos = start - base;
    if (start == 0 || buffer[pos - 1] != '\n') {
        const auto* nl = static_cast<const char*>(std::memchr(buffer.data() + pos, '\n', logical_end - pos));
        if (!nl) return;
        pos = static_cast<size_t>(nl - buffer.data()) + 1;
    }
    if (pos >= logical_end) return;
    size_t look = logical_end - 1;
    for (;;) {
        const auto* nl = static_cast<const char*>(std::memchr(buffer.data() + look, '\n', end - look));
        if (nl) {
            size_t length = static_cast<size_t>(nl - buffer.data()) + 1 - pos;
            parse_records(buffer.data() + pos, length, process);
            return;
        }
        if (end == file.bytes - base) {
            buffer.push_back('\n');
            parse_records(buffer.data() + pos, buffer.size() - pos, process);
            return;
        }
        look = end;
        end = std::min(file.bytes - base, end + input_chunk);
        buffer.resize(end);
        read_bytes(file, base + look, end - look, buffer.data() + look);
    }
}

struct IngestDates {
    struct Item {
        char date[10]{};
        int64_t seconds = 0;
    };
    std::array<Item, 32> table{};
    int64_t seconds(std::string_view s) {
        // The spec guarantees that every retained ingest timestamp is valid UTC F1.
        const char* p = s.data();
        auto& item = table[(p[8] * 10 + p[9]) & 31];
        if (std::memcmp(item.date, p, 10)) {
            auto t = parse_timestamp(s) / 1000;
            item.seconds = (t / 86400 - (t % 86400 < 0)) * 86400;
            std::memcpy(item.date, p, 10);
        }
        return item.seconds + digit_pair(p + 11) * 3600 + digit_pair(p + 14) * 60 + digit_pair(p + 17);
    }
};
struct alignas(128) Reader {
    std::vector<Partition> partitions;
    std::vector<char> buffer;
    IngestDates dates;
    struct PendingName {
        Candidate* candidate;
        size_t offset, length;
    };
    std::array<PendingName, Inputs::lookup_batch_size> pending;
    size_t used = 0;

    void flush(const Inputs& inputs) {
        if (!used) return;
        std::array<std::string_view, Inputs::lookup_batch_size> names;
        std::array<uint32_t, Inputs::lookup_batch_size> ids;
        for (size_t i = 0; i < used; ++i)
            names[i] = {buffer.data() + pending[i].offset, pending[i].length};
        inputs.resolve_people(names.data(), ids.data(), used);
        for (size_t i = 0; i < used; ++i) pending[i].candidate->person = ids[i];
        used = 0;
    }
    void queue(const Inputs& inputs, Candidate& c, std::string_view name) {
        // Offsets survive buffer growth for a long record. read_chunk flushes before the next overwrite.
        pending[used++] = {&c, static_cast<size_t>(name.data() - buffer.data()), name.size()};
        if (used == pending.size()) flush(inputs);
    }
};

void parse_line(const Inputs& inputs, Reader& reader, const std::array<std::string_view, 7>& f, size_t buckets) {
    if (f[0].empty()) return;
    const Id id = identify(f[0]);
    auto& part = reader.partitions[(id.hash >> 32) & (buckets - 1)];
    uint64_t key = id.mode ? id.word : part.save_string(f[0], static_cast<uint32_t>(id.hash));
    Candidate row{key, 0, absent, 0, id.mode << 28};

    if (f[3].size() != 20) throw std::runtime_error("ingest timestamp violates F1 input guarantee");
    const int64_t ingest = reader.dates.seconds(f[3]);
    const int64_t encoded = ingest - inputs.window_start / 1000 + INT32_MAX;
    row.ingest = encoded >= 0 && encoded < UINT32_MAX ? static_cast<uint32_t>(encoded) : absent;

    const int64_t ts = parse_timestamp(f[2]);
    const bool purchase = f[4] == "purchase";
    const uint32_t cents = purchase ? parse_price(f[6]) : 0;
    const bool type_ok = purchase ? cents > 0 : f[4] == "view" || f[4] == "cart";
    const bool valid = !f[1].empty() && !f[5].empty() && type_ok &&
                       ts != invalid_timestamp && ts >= inputs.window_start && ts < inputs.window_end &&
                       ingest * 1000 - ts <= 86400000;

    bool wide = false;
    uint32_t category = 0;
    if (valid) {
        uint64_t offset = static_cast<uint64_t>(ts - inputs.window_start);
        row.offset = static_cast<uint32_t>(offset);
        wide = offset > UINT32_MAX;
        if (purchase) category = inputs.product_category(f[5]);
    }
    if ((valid && (purchase || wide)) || row.ingest == absent) {
        part.details.push_back({ingest, ts, category, cents});
        if (part.details.size() > detail_mask)
            throw std::length_error("too many event details in one partition");
        row.meta |= static_cast<uint32_t>(part.details.size());
    }
    // Even an invalid event participates in deduplication. Its absent person ID
    // suppresses a later valid copy when this is the earliest ingestion.
    Candidate& saved = part.push(row);
    if (valid) reader.queue(inputs, saved, f[1]);
}

// Ingestion timestamps normally fit in a biased 32-bit seconds offset. Outliers
// retain the full 64-bit value in the detail record, including for invalid events.
bool earlier(const Partition& current_part, const Candidate& current,
             const Partition& previous_part, const Candidate& previous, int64_t base) {
    if (current.ingest != absent && previous.ingest != absent)
        return current.ingest < previous.ingest;
    int64_t current_time = current.ingest == absent ? current_part.detail(current).ingest : base + current.ingest;
    int64_t previous_time = previous.ingest == absent ? previous_part.detail(previous).ingest : base + previous.ingest;
    return current_time < previous_time;
}

struct Slot {
    Candidate* winner = nullptr;
    uint32_t reader = 0, hash = 0;
};

struct alignas(128) DedupScratch {
    std::vector<uint64_t> filter;
    std::vector<uint32_t> hashes;
    std::vector<Slot> slots;
};

void deduplicate(std::vector<Reader>& readers, size_t bucket, WorkerEvents& output,
                 int64_t window_start, DedupScratch& scratch) {
    size_t count = 0;
    for (const auto& reader : readers) count += reader.partitions[bucket].count;
    // A two-bit counter saturates on the second observation. A unique
    // counter proves its only row cannot have a duplicate, even across readers.
    size_t filter_words = 1;
    while (filter_words * 32 < count * 8) filter_words *= 2;
    auto& filter = scratch.filter;
    auto& hashes = scratch.hashes;
    filter.assign(filter_words, 0);
    hashes.clear();
    hashes.reserve(count);
    size_t suspects = 0;
    auto bit = [](uint32_t hash) { return uint64_t{1} << (2 * (hash & 31)); };
    for (auto& reader : readers) {
        auto& part = reader.partitions[bucket];
        part.each([&](const Candidate& row) {
            const uint32_t hash = part.slot_hash(row);
            hashes.push_back(hash);
            const uint64_t mask = bit(hash);
            auto& word = filter[(hash >> 5) & (filter_words - 1)];
            if (word & mask) {
                // The first collision sends both rows to the exact table.
                suspects += (word & (mask << 1)) ? 1 : 2;
                word |= mask << 1;
            } else {
                word |= mask;
            }
        });
    }
    size_t capacity = 4;
    while (capacity < suspects + suspects / 3) capacity *= 2;
    auto& slots = scratch.slots;
    slots.assign(capacity, {});
    size_t ordinal = 0;
    const int64_t ingest_base = window_start / 1000 - INT32_MAX;
    for (uint32_t k = 0; k < readers.size(); ++k) {
        auto& part = readers[k].partitions[bucket];
        part.each([&](Candidate& row) {
            const uint32_t hash = hashes[ordinal++];
            if (!(filter[(hash >> 5) & (filter_words - 1)] & (bit(hash) << 1))) return;
            size_t index = hash & (capacity - 1);
            while (slots[index].winner) {
                auto& slot = slots[index];
                auto& incumbent = *slot.winner;
                if (hash == slot.hash && row.id_mode() == incumbent.id_mode()) {
                    auto& owner = readers[slot.reader].partitions[bucket];
                    bool same = row.id_mode() ? row.key == incumbent.key :
                                               part.string(row.key) == owner.string(incumbent.key);
                    if (same) {
                        if (earlier(part, row, owner, incumbent, ingest_base)) {
                            incumbent.person = absent;
                            slot = {&row, k, hash};
                        } else {
                            row.person = absent;
                        }
                        return;
                    }
                }
                index = (index + 1) & (capacity - 1);
            }
            slots[index] = {&row, k, hash};
        });
    }
    for (auto& reader : readers) {
        auto& part = reader.partitions[bucket];
        part.each([&](const Candidate& row) {
            if (row.person == absent) return;
            Event event{window_start + row.offset, row.person, 0, 0};
            if (row.detail_index()) {
                const auto& detail = part.detail(row);
                event = {detail.ts, row.person, detail.category, detail.price};
            }
            output.add(window_start, event);
        });
        part = {};
    }
}

} // namespace

void read_events(const std::filesystem::path& data_dir, const Inputs& inputs,
                 std::vector<WorkerEvents>& workers) {
    const size_t threads = workers.size();
    const bool profile = std::getenv("SHOP_PROFILE") != nullptr;
    auto stage_start = std::chrono::steady_clock::now();
    auto checkpoint = [&](const char* stage) {
        if (!profile) return;
        auto now = std::chrono::steady_clock::now();
        std::cerr << stage << " " << std::chrono::duration<double>(now - stage_start).count() << " s\n";
        stage_start = now;
    };
    struct Task { size_t file, offset; };
    std::vector<File> files;
    std::vector<Task> tasks;
    size_t bytes = 0;
    for (auto& entry : std::filesystem::directory_iterator(data_dir / "events")) {
        if (entry.path().extension() != ".csv") continue;
        size_t file = files.size();
        files.emplace_back(entry.path());
        bytes += files.back().bytes;
        for (size_t offset = 0; offset < files.back().bytes; offset += input_chunk)
            tasks.push_back({file, offset});
    }
    size_t buckets = 1;
    while (buckets < 1024 && buckets * (4u << 20) < bytes) buckets *= 2;
    std::vector<Reader> readers(threads);
    for (auto& r : readers) r.partitions.resize(buckets);
    run_jobs(threads, tasks.size(), [&](size_t worker, size_t i) {
        auto& r = readers[worker];
        auto task = tasks[i];
        read_chunk(files[task.file], task.offset, r.buffer, [&](const auto& fields) {
            parse_line(inputs, r, fields, buckets);
        });
        r.flush(inputs);
    });
    checkpoint("stream CSV and identify");
    size_t rows = 0;
    for (auto& r : readers) {
        for (auto& p : r.partitions) rows += p.count;
        std::vector<char>().swap(r.buffer);
    }
    for (auto& worker : workers) worker.reserve(inputs.person_count(), rows / threads + 1);
    std::vector<DedupScratch> scratch(threads);
    run_jobs(threads, buckets, [&](size_t worker, size_t bucket) {
        deduplicate(readers, bucket, workers[worker], inputs.window_start, scratch[worker]);
    });
    checkpoint("stream deduplicate and collect");
}

} // namespace shop
