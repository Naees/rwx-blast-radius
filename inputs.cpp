#include "inputs.hpp"
#include "csv.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <filesystem>
#include <fstream>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace shop {
namespace {
using View = std::string_view;
namespace fs = std::filesystem;
constexpr uint32_t absent = std::numeric_limits<uint32_t>::max();

class Mapping {
public:
    explicit Mapping(const fs::path& path) {
        int fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) throw std::runtime_error("Cannot open " + path.string());
        struct stat st;
        if (::fstat(fd, &st) != 0) {
            ::close(fd);
            throw std::runtime_error("Cannot stat " + path.string());
        }
        if (st.st_size) {
            void* address = ::mmap(nullptr, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
            ::close(fd);
            if (address == MAP_FAILED) throw std::runtime_error("Cannot map " + path.string());
            bytes = View(static_cast<const char*>(address), st.st_size);
        } else {
            ::close(fd);
        }
    }
    Mapping(const Mapping&) = delete;
    ~Mapping() { if (!bytes.empty()) ::munmap(const_cast<char*>(bytes.data()), bytes.size()); }
    View bytes;
};

size_t next_line(View bytes, size_t from) {
    size_t end = bytes.find('\n', from);
    return end == View::npos ? bytes.size() : end + 1;
}

template<class Fn>
void each_line(View bytes, Fn&& fn) {
    size_t at = 0;
    while (at < bytes.size()) {
        size_t end = bytes.find('\n', at);
        if (end == View::npos) end = bytes.size();
        fn(bytes.substr(at, end - at));
        at = end + 1;
    }
}

// Keys point into a retained mapping or into stable storage for singleton users.
// An empty key is invalid in every table that uses this index.
struct Entry {
    View key;
    uint32_t value = 0;
};

class Index {
public:
    void reserve(size_t count) {
        size_t capacity = entries_.size();
        while (count > capacity * 7 / 10) capacity *= 2;
        if (capacity == entries_.size()) return;
        std::vector<Entry> previous(capacity);
        previous.swap(entries_);
        for (const Entry& e : previous) {
            if (!e.key.empty()) entries_[position(e.key, hash(e.key))] = e;
        }
    }
    static size_t hash(View key) { return std::hash<View>{}(key); }

    Entry get(View key, size_t h) const {
        return entries_[position(key, h)];
    }
    uint32_t get(View key) const {
        Entry entry = get(key, hash(key));
        return entry.key.empty() ? absent : entry.value;
    }
    void put(Entry e, size_t h) {
        reserve(size_ + 1);
        Entry& destination = entries_[position(e.key, h)];
        size_ += destination.key.empty();
        destination = e;
    }
    void prefetch(size_t hash) const { __builtin_prefetch(&entries_[hash & (entries_.size() - 1)]); }
    std::vector<Entry>& entries() { return entries_; }

private:
    size_t position(View key, size_t h) const {
        size_t mask = entries_.size() - 1, index = h & mask;
        while (!entries_[index].key.empty() && entries_[index].key != key)
            index = (index + 1) & mask;
        return index;
    }
    std::vector<Entry> entries_{16};
    size_t size_ = 0;
};

struct Probe {
    uint64_t word, hash;
    uint32_t tag;
};

uint64_t hash_short_name(uint64_t word, uint32_t tag) {
    uint64_t mixed = (word ^ (uint64_t(tag) << 47)) * UINT64_C(0x9e3779b185ebca87);
    mixed = (mixed ^ (mixed >> 32)) * UINT64_C(0xc2b2ae3d27d4eb4f);
    return mixed ^ (mixed >> 31);
}

// Pack up to nine bytes losslessly into a word plus the length and first byte.
// Exact lookups for short names need no extra read from the backing CSV.
Probe name_probe(View name) {
    const size_t n = name.size();
    if (n == 0 || n > 9) return {0, Index::hash(name), 0};
    uint64_t word = 0;
    if (n >= 8) {
        std::memcpy(&word, name.data() + n - 8, 8);
    } else if (n >= 4) {
        uint32_t left, right;
        std::memcpy(&left, name.data(), 4);
        std::memcpy(&right, name.data() + n - 4, 4);
        word = (uint64_t(left) << 32) | right;
    } else {
        for (unsigned char c : name) word = (word << 8) | c;
    }
    auto tag = static_cast<uint32_t>((n << 8) | static_cast<unsigned char>(name[0]));
    return {word, hash_short_name(word, tag), tag};
}

class PersonIndex {
    struct Slot {
        uint64_t word = 0;
        uint32_t tag = 0, value = 0;
    };
    std::vector<Slot> short_{16};
    Index long_;
    size_t count_ = 0;

    size_t position(const Probe& k) const {
        size_t mask = short_.size() - 1, index = k.hash & mask;
        while (short_[index].tag &&
               (short_[index].tag != k.tag || short_[index].word != k.word))
            index = (index + 1) & mask;
        return index;
    }

public:
    void reserve(size_t count) {
        size_t capacity = short_.size();
        while (count > capacity * 7 / 10) capacity *= 2;
        if (capacity == short_.size()) return;
        std::vector<Slot> old(capacity);
        old.swap(short_);
        for (const Slot& e : old) {
            if (e.tag) short_[position({e.word, hash_short_name(e.word, e.tag), e.tag})] = e;
        }
    }
    void prefetch(const Probe& k) const {
        if (k.tag) __builtin_prefetch(&short_[k.hash & (short_.size() - 1)]);
        else long_.prefetch(k.hash);
    }
    uint32_t get(View name, const Probe& k, View* stable = nullptr) const {
        if (k.tag) {
            const auto& slot = short_[position(k)];
            return slot.tag ? slot.value : absent;
        }
        auto e = long_.get(name, k.hash);
        if (stable) *stable = e.key;
        return e.key.empty() ? absent : e.value;
    }
    void put(View name, const Probe& k, uint32_t value) {
        if (!k.tag) {
            long_.put({name, value}, k.hash);
            return;
        }
        reserve(count_ + 1);
        Slot& slot = short_[position(k)];
        count_ += !slot.tag;
        slot = {k.word, k.tag, value};
    }
    template<class Fn>
    void transform_values(Fn&& fn) {
        for (Slot& e : short_) {
            if (e.tag) e.value = fn(e.value);
        }
        for (Entry& e : long_.entries()) {
            if (!e.key.empty()) e.value = fn(e.value);
        }
    }
};

struct Components {
    std::vector<uint32_t> parent, sizes;
    std::vector<View> smallest;
    uint32_t add(View name) {
        uint32_t id = static_cast<uint32_t>(parent.size());
        parent.push_back(id);
        sizes.push_back(1);
        smallest.push_back(name);
        return id;
    }
    uint32_t root(uint32_t id) {
        while (id != parent[id]) {
            parent[id] = parent[parent[id]];
            id = parent[id];
        }
        return id;
    }
    void join(uint32_t a, uint32_t b) {
        a = root(a);
        b = root(b);
        if (a == b) return;
        if (sizes[a] < sizes[b]) std::swap(a, b);
        parent[b] = a;
        sizes[a] += sizes[b];
        smallest[a] = std::min(smallest[a], smallest[b]);
    }
};

int64_t manifest_time(const std::string& json, const std::string& key) {
    auto pos = json.find('"' + key + '"');
    if (pos != json.npos) pos = json.find(':', pos + key.size() + 2);
    if (pos != json.npos) pos = json.find('"', pos + 1);
    if (pos != json.npos) {
        auto end = json.find('"', pos + 1);
        if (end != json.npos) {
            int64_t value = parse_timestamp(View(json).substr(pos + 1, end - pos - 1));
            if (value != INT64_MIN) return value;
        }
    }
    throw std::runtime_error("Invalid manifest " + key);
}

unsigned worker_count(size_t tasks) {
    const char* setting = std::getenv("NCPU");
    int available = setting ? std::atoi(setting) : 8;
    return static_cast<unsigned>(std::min<size_t>(std::clamp(available, 1, 64), tasks));
}
std::atomic<uint64_t> next_generation{1};
} // namespace

struct Inputs::State {
    std::vector<std::unique_ptr<Mapping>> mappings;
    PersonIndex aliases;
    Index products, campaign_ids;
    std::vector<View> names;
    std::mutex names_mutex;
    struct alignas(64) Shard {
        std::mutex mutex;
        Index singletons;
        std::deque<std::string> strings;
    };
    std::array<Shard, 64> shards;
    uint64_t generation = next_generation.fetch_add(1, std::memory_order_relaxed);

    View rows(const fs::path& path) {
        mappings.push_back(std::make_unique<Mapping>(path));
        View raw = mappings.back()->bytes;
        return raw.substr(next_line(raw, 0));
    }

    std::vector<View> parts(const fs::path& path) {
        std::vector<View> result;
        for (const auto& file : fs::directory_iterator(path)) {
            if (file.path().extension() == ".csv" && file.is_regular_file())
                result.push_back(rows(file.path()));
        }
        return result;
    }

    void load_links(const fs::path& path) {
        Components components;
        auto files = parts(path);
        size_t rows = 0;
        for (View file : files) {
            rows += std::count(file.begin(), file.end(), '\n');
            rows += !file.empty() && file.back() != '\n';
        }
        // Long IDs and malformed lines can occupy much more space than valid edges.
        aliases.reserve(std::min(rows + rows / 3, size_t{1} << 22));
        auto vertex = [&](View name) {
            auto key = name_probe(name);
            uint32_t old = aliases.get(name, key);
            if (old != absent) return old;
            uint32_t id = components.add(name);
            aliases.put(name, key, id);
            return id;
        };
        for (View file : files) {
            each_line(file, [&](View line) {
                std::array<View, 2> fields;
                if (parse_csv(line, fields) && !fields[0].empty() && !fields[1].empty())
                    components.join(vertex(fields[0]), vertex(fields[1]));
            });
        }
        std::vector<uint32_t> component_ids(components.parent.size(), absent);
        names.reserve(components.parent.size());
        aliases.transform_values([&](uint32_t id) {
            uint32_t root = components.root(id);
            uint32_t& person = component_ids[root];
            if (person == absent) {
                person = static_cast<uint32_t>(names.size());
                names.push_back(components.smallest[root]);
            }
            return person;
        });
    }

    uint32_t resolve_person(View id, const Probe& key);

    Entry register_person(View key, size_t hash) {
        Entry known;
        Shard& shard = shards[hash >> (sizeof(size_t) * 8 - 6)];
        std::lock_guard<std::mutex> lock(shard.mutex);
        known = shard.singletons.get(key, hash);
        if (!known.key.empty()) return known;
        shard.strings.emplace_back(key);
        View stable = shard.strings.back();
        {
            std::lock_guard<std::mutex> name_lock(names_mutex);
            known = {stable, static_cast<uint32_t>(names.size())};
            names.push_back(stable);
        }
        shard.singletons.put(known, hash);
        return known;
    }
};

Inputs::Inputs(const std::string& data_dir) : state_(std::make_unique<State>()) {
    fs::path dir(data_dir);
    std::ifstream manifest(dir / "manifest.json");
    std::string json((std::istreambuf_iterator<char>(manifest)), {});
    window_start = manifest_time(json, "window_start");
    window_end = manifest_time(json, "window_end");

    std::unordered_map<View, uint32_t> category_ids{{"unknown", 0}};
    categories.emplace_back("unknown");
    each_line(state_->rows(dir / "products.csv"), [&](View line) {
        std::array<View, 2> fields;
        if (!parse_csv(line, fields) || fields[0].empty()) return;
        auto [it, added] = category_ids.emplace(fields[1], categories.size());
        if (added) categories.emplace_back(fields[1]);
        state_->products.put({fields[0], it->second}, Index::hash(fields[0]));
    });
    campaigns.emplace_back("organic");
    channels.emplace_back("organic");
    each_line(state_->rows(dir / "campaigns.csv"), [&](View line) {
        std::array<View, 2> fields;
        if (!parse_csv(line, fields) || fields[0].empty()) return;
        state_->campaign_ids.put({fields[0], static_cast<uint32_t>(campaigns.size())},
                                 Index::hash(fields[0]));
        campaigns.emplace_back(fields[0]);
        channels.emplace_back(fields[1]);
    });
    state_->load_links(dir / "links");

    constexpr size_t chunk_size = 1 << 20;
    std::vector<View> chunks;
    for (View part : state_->parts(dir / "clicks")) {
        for (size_t start = 0; start < part.size();) {
            size_t end = start + chunk_size < part.size()
                       ? next_line(part, start + chunk_size) : part.size();
            chunks.push_back(part.substr(start, end - start));
            start = end;
        }
    }
    std::vector<std::vector<Click>> parsed(chunks.size());
    std::atomic<size_t> next{0};
    std::exception_ptr failure;
    std::mutex failure_mutex;
    const int64_t first_click = window_start - 604800000;
    auto parse_chunks = [&] {
        try {
            for (size_t i; (i = next.fetch_add(1, std::memory_order_relaxed)) < chunks.size();) {
                std::vector<Click> out;
                out.reserve(chunks[i].size() / 55);
                each_line(chunks[i], [&](View line) {
                    std::array<View, 4> fields;
                    if (!parse_csv(line, fields) || fields[0].empty() || fields[1].empty()) return;
                    uint32_t campaign = state_->campaign_ids.get(fields[2]);
                    if (campaign == absent) return;
                    int64_t ts = parse_timestamp(fields[3]);
                    if (ts < first_click || ts >= window_end) return;
                    out.push_back({person(fields[1]), campaign, ts, fields[0]});
                });
                parsed[i] = std::move(out);
            }
        } catch (...) {
            std::lock_guard<std::mutex> lock(failure_mutex);
            failure = std::current_exception();
        }
    };
    std::vector<std::thread> workers;
    for (unsigned i = 1; i < worker_count(chunks.size()); ++i) workers.emplace_back(parse_chunks);
    parse_chunks();
    for (auto& worker : workers) worker.join();
    if (failure) std::rethrow_exception(failure);
    size_t count = 0;
    for (const auto& part : parsed) count += part.size();
    clicks.reserve(count);
    for (auto& part : parsed) clicks.insert(clicks.end(), part.begin(), part.end());

    // Event threads read these users from the immutable index as well.
    for (auto& shard : state_->shards) {
        for (const Entry& e : shard.singletons.entries()) {
            if (!e.key.empty()) state_->aliases.put(e.key, name_probe(e.key), e.value);
        }
        shard.singletons = Index{};
    }
}

Inputs::~Inputs() = default;
uint32_t Inputs::State::resolve_person(View id, const Probe& key) {
    struct Cached {
        uint64_t word = 0;
        uint32_t tag = 0, person = 0;
        View long_key;
    };
    struct Cache {
        uint64_t generation = 0;
        std::array<Cached, 4096> slots{};
    };
    static thread_local Cache cache;
    if (cache.generation != generation) {
        cache.slots.fill({});
        cache.generation = generation;
    }
    Cached& cached = cache.slots[key.hash & (cache.slots.size() - 1)];
    if (key.tag) {
        if (cached.tag == key.tag && cached.word == key.word) return cached.person;
    } else if (cached.tag == 0 && cached.long_key == id) {
        return cached.person;
    }
    uint32_t person_id = aliases.get(id, key, &cached.long_key);
    if (person_id == absent) {
        auto entry = register_person(id, Index::hash(id));
        cached.long_key = key.tag ? View{} : entry.key;
        person_id = entry.value;
    }
    cached.word = key.word;
    cached.tag = key.tag;
    cached.person = person_id;
    return person_id;
}
uint32_t Inputs::person(View id) const { return state_->resolve_person(id, name_probe(id)); }

void Inputs::resolve_people(const View* names, uint32_t* resolved, size_t count) const {
    // Issue independent reads before resolving the cached or newly interned names.
    while (count) {
        const size_t n = std::min(count, lookup_batch_size);
        std::array<Probe, lookup_batch_size> probes;
        for (size_t i = 0; i < n; ++i) {
            probes[i] = name_probe(names[i]);
            state_->aliases.prefetch(probes[i]);
        }
        for (size_t i = 0; i < n; ++i)
            resolved[i] = state_->resolve_person(names[i], probes[i]);
        names += n;
        resolved += n;
        count -= n;
    }
}
View Inputs::person_name(uint32_t id) const { return state_->names.at(id); }
uint32_t Inputs::person_count() const { return static_cast<uint32_t>(state_->names.size()); }
uint32_t Inputs::product_category(View product) const {
    uint32_t category = state_->products.get(product);
    return category == absent ? 0 : category;
}

} // namespace shop
