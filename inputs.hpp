#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace shop {

struct Click {
    uint32_t person;
    uint32_t campaign;
    int64_t ts;
    std::string_view id;
};

class Inputs {
public:
    explicit Inputs(const std::string& data_dir);
    ~Inputs();

    static constexpr std::size_t lookup_batch_size = 32;
    uint32_t person(std::string_view id) const;
    void resolve_people(const std::string_view* names, uint32_t* resolved, std::size_t count) const;
    // Access person_name and person_count once all calls that register users finish.
    std::string_view person_name(uint32_t person_id) const;
    uint32_t person_count() const;
    uint32_t product_category(std::string_view product) const;

    int64_t window_start;
    int64_t window_end;
    // Index 0 is "unknown" for categories and "organic" for campaigns and channels.
    std::vector<std::string> categories, campaigns, channels;
    // Exact duplicate clicks may remain. The backing bytes of each id live with Inputs.
    std::vector<Click> clicks;

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace shop
