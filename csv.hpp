#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string_view>

#if defined(__AVX2__)
#include <immintrin.h>
#elif defined(__SSE2__)
#include <emmintrin.h>
#elif defined(__aarch64__)
#include <arm_neon.h>
#endif

namespace shop {

namespace detail {
#if defined(__AVX2__)
constexpr int vector_bytes = 32;
inline uint32_t delimiter_mask(const char* p, char target) {
    const auto bytes = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(p));
    return _mm256_movemask_epi8(_mm256_cmpeq_epi8(bytes, _mm256_set1_epi8(target)));
}
#elif defined(__SSE2__)
constexpr int vector_bytes = 16;
inline uint32_t delimiter_mask(const char* p, char target) {
    const auto bytes = _mm_loadu_si128(reinterpret_cast<const __m128i*>(p));
    return _mm_movemask_epi8(_mm_cmpeq_epi8(bytes, _mm_set1_epi8(target)));
}
#elif defined(__aarch64__)
constexpr int vector_bytes = 16;
inline unsigned byte_mask(uint8x16_t bytes) {
    const uint8x16_t bits = {1,2,4,8,16,32,64,128,1,2,4,8,16,32,64,128};
    const auto selected = vandq_u8(bytes, bits);
    const auto pairs = vpaddlq_u8(selected);
    const auto quads = vpaddlq_u16(pairs);
    const auto octets = vpaddlq_u32(quads);
    return static_cast<unsigned>(vgetq_lane_u64(octets, 0) | (vgetq_lane_u64(octets, 1) << 8));
}
inline uint32_t delimiter_mask(const char* p, char target) {
    const auto bytes = vld1q_u8(reinterpret_cast<const uint8_t*>(p));
    return byte_mask(vceqq_u8(bytes, vdupq_n_u8(static_cast<uint8_t>(target))));
}
#endif

#if defined(__AVX2__) || defined(__SSE2__) || defined(__aarch64__)
inline void csv_masks(const char* p, uint32_t& commas, uint32_t& quotes) {
    commas = delimiter_mask(p, ',');
    quotes = delimiter_mask(p, '"');
}

// Three masks cover common rows. Unusually long rows use the general parser.
template <std::size_t N>
inline bool parse_short_csv(std::string_view line, std::array<std::string_view, N>& fields) {
    uint64_t commas[3]{};
    unsigned quote_count = 0;
    const char* data = line.data();
    std::size_t i = 0;
    uint32_t c, q;
    for (; i + vector_bytes <= line.size(); i += vector_bytes) {
        csv_masks(data + i, c, q);
        commas[i / 64] |= uint64_t(c) << (i % 64);
        quote_count += __builtin_popcount(q);
    }
    if (i < line.size()) {
        // An overlapping load keeps every byte inside the row, including at mmap boundaries.
        csv_masks(data + line.size() - vector_bytes, c, q);
        const unsigned skip = static_cast<unsigned>(i + vector_bytes - line.size());
        commas[i / 64] |= uint64_t(c >> skip) << (i % 64);
        quote_count += __builtin_popcount(q >> skip);
    }
    std::size_t start = 0, group = 0;
    for (std::size_t j = 0; j < N; ++j) {
        std::size_t stop = line.size();
        if (j + 1 != N) {
            while (!commas[group]) if (++group == 3) return false;
            stop = group * 64 + __builtin_ctzll(commas[group]);
            commas[group] &= commas[group] - 1;
        }
        const bool quoted = stop - start >= 2 && data[start] == '"' && data[stop-1] == '"';
        fields[j] = std::string_view(data + start + quoted, stop - start - 2 * quoted);
        quote_count -= 2 * quoted;
        start = stop + 1;
    }
    return !(commas[0] | commas[1] | commas[2] | quote_count);
}
#endif
} // namespace detail


template <std::size_t N>
inline bool parse_csv(std::string_view line, std::array<std::string_view, N>& fields) {
    static_assert(N > 0, "CSV records require at least one field");
    if (line.empty()) return false;
#if defined(__AVX2__) || defined(__SSE2__) || defined(__aarch64__)
    if (line.size() >= detail::vector_bytes && line.size() <= 192)
        return detail::parse_short_csv(line, fields);
#endif
    const char* start = line.data();
    const char* end = start + line.size();
    const bool has_quote = std::memchr(start, '"', line.size()) != nullptr;
    for (std::size_t i = 0; i < N; ++i) {
        const char* next = static_cast<const char*>(std::memchr(start, ',', end - start));
        if ((i + 1 == N) != (next == nullptr)) return false;
        const char* stop = next ? next : end;
        const char* field = start;
        const char* after = stop;
        if (has_quote) {
            if (after - field >= 2 && *field == '"' && after[-1] == '"') {
                ++field;
                --after;
            }
            if (std::memchr(field, '"', after - field) != nullptr) return false;
        }
        fields[i] = std::string_view(field, static_cast<std::size_t>(after - field));
        if (next) start = next + 1;
    }
    return true;
}

inline constexpr int64_t invalid_timestamp = std::numeric_limits<int64_t>::min();

inline unsigned digit_pair(const char* p) {
    unsigned first = static_cast<unsigned char>(p[0]) - static_cast<unsigned>('0');
    unsigned second = static_cast<unsigned char>(p[1]) - static_cast<unsigned>('0');
    return (first > 9 || second > 9) ? 100 : first * 10 + second;
}

inline int64_t days_from_civil(int year, unsigned month, unsigned day) {
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const int year_in_era = year - era * 400;
    const int march_month = static_cast<int>(month) + (month > 2 ? -3 : 9);
    const int day_in_year = (153 * march_month + 2) / 5 + static_cast<int>(day) - 1;
    return int64_t(era) * 146097 + year_in_era * 365 + year_in_era / 4 - year_in_era / 100 + day_in_year - 719468;
}

inline int64_t parse_timestamp(std::string_view value) {
    const char* p = value.data();
    const std::size_t n = value.size();
    if ((n == 20 || n == 25) && p[4] == '-') {
        if (p[7] != '-' || p[10] != 'T' || p[13] != ':' || p[16] != ':') return invalid_timestamp;
        int offset_minutes = 0;
        if (n == 20) {
            if (p[19] != 'Z') return invalid_timestamp;
        } else {
            if ((p[19] != '+' && p[19] != '-') || p[22] != ':') return invalid_timestamp;
            const auto h = digit_pair(p + 20), m = digit_pair(p + 23);
            if (h > 23 || m > 59) return invalid_timestamp;
            offset_minutes = static_cast<int>(h * 60 + m) * (p[19] == '+' ? 1 : -1);
        }
        const unsigned century = digit_pair(p), yy = digit_pair(p + 2), month = digit_pair(p + 5);
        const unsigned day = digit_pair(p + 8), hour = digit_pair(p + 11), minute = digit_pair(p + 14), second = digit_pair(p + 17);
        if (century >= 100 || yy >= 100 || month - 1 >= 12 || hour >= 24 || minute >= 60 || second >= 60) return invalid_timestamp;
        const int year = static_cast<int>(century * 100 + yy);
        constexpr unsigned lengths[] = {31,28,31,30,31,30,31,31,30,31,30,31};
        const bool leap = (year % 4 == 0) && (year % 100 != 0 || year % 400 == 0);
        if (day == 0 || day > lengths[month - 1] + (month == 2 && leap)) return invalid_timestamp;
        int64_t seconds = days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second;
        return (seconds - offset_minutes * 60) * 1000;
    }
#if defined(__AVX2__)
    if (n == 13) {
        // Epoch milliseconds commonly have 13 digits. Two bounded, overlapping
        // eight-byte loads allow every byte to be checked in one vector.
        const auto first = _mm_loadl_epi64(reinterpret_cast<const __m128i*>(p));
        const auto last = _mm_loadl_epi64(reinterpret_cast<const __m128i*>(p + 5));
        const auto digits = _mm_sub_epi8(_mm_unpacklo_epi64(first, last), _mm_set1_epi8('0'));
        const auto overflow = _mm_subs_epu8(digits, _mm_set1_epi8(9));
        if (!_mm_testz_si128(overflow, overflow)) return invalid_timestamp;
        const auto pairs = _mm_maddubs_epi16(digits, _mm_set1_epi16(0x010a));
        const auto quads = _mm_madd_epi16(pairs, _mm_set1_epi32(0x00010064));
        const uint32_t leading_eight = _mm_extract_epi32(quads, 0) * 10000 + _mm_extract_epi32(quads, 1);
        const uint32_t trailing_eight = _mm_extract_epi32(quads, 2) * 10000 + _mm_extract_epi32(quads, 3);
        return int64_t(leading_eight / 1000) * 100000000 + trailing_eight;
    }
#endif
    if (value.empty()) return invalid_timestamp;
    int64_t ms = 0;
    if (n <= 18) {
        for (unsigned char c : value) {
            const unsigned d = c - static_cast<unsigned>('0');
            if (d > 9) return invalid_timestamp;
            ms = ms * 10 + d;
        }
    } else {
        for (unsigned char c : value) {
            const unsigned d = c - static_cast<unsigned>('0');
            if (d > 9 || ms > (std::numeric_limits<int64_t>::max() - d) / 10) return invalid_timestamp;
            ms = ms * 10 + d;
        }
    }
    return ms;
}

} // namespace shop
