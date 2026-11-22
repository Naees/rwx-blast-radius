#include "output.hpp"

#include <arrow/io/file.h>
#include <parquet/file_writer.h>
#include <parquet/column_writer.h>

#include <algorithm>
#include <future>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace shop {
namespace {
using parquet::ConvertedType;
using parquet::Repetition;
using parquet::Type;
using parquet::schema::GroupNode;
using parquet::schema::NodePtr;
using parquet::schema::PrimitiveNode;

NodePtr field(const char* name, Type::type type, ConvertedType::type annotation = ConvertedType::NONE,
              Repetition::type repetition = Repetition::REQUIRED) {
    return PrimitiveNode::Make(name, repetition, type, annotation);
}

std::shared_ptr<GroupNode> schema(bool users) {
    parquet::schema::NodeVector fields;
    if (users) {
        fields = {field("user_id", Type::BYTE_ARRAY, ConvertedType::UTF8),
                  field("day", Type::INT32, ConvertedType::DATE), field("sessions", Type::INT32),
                  field("events", Type::INT32), field("revenue_cents", Type::INT64),
                  field("top_category", Type::BYTE_ARRAY, ConvertedType::UTF8, Repetition::OPTIONAL)};
    } else {
        fields = {field("campaign_id", Type::BYTE_ARRAY, ConvertedType::UTF8),
                  field("channel", Type::BYTE_ARRAY, ConvertedType::UTF8),
                  field("day", Type::INT32, ConvertedType::DATE), field("sessions", Type::INT32),
                  field("purchasing_sessions", Type::INT32), field("revenue_cents", Type::INT64),
                  field("assisted_sessions", Type::INT32)};
    }
    return std::static_pointer_cast<GroupNode>(GroupNode::Make("shopping", Repetition::REQUIRED, fields));
}

// The Parquet writer consumes each values buffer synchronously.
class Columns {
public:
    explicit Columns(parquet::RowGroupWriter* group) : group_(group) {}

    template<class Row, class T>
    void number(const Row* rows, size_t size, T Row::*member) {
        if constexpr (sizeof(T) == 8) {
            auto* column = static_cast<parquet::Int64Writer*>(group_->NextColumn());
            wide_.resize(size);
            for (size_t i = 0; i < size; ++i) wide_[i] = rows[i].*member;
            column->WriteBatch(size, nullptr, nullptr, wide_.data());
        } else {
            auto* column = static_cast<parquet::Int32Writer*>(group_->NextColumn());
            narrow_.resize(size);
            for (size_t i = 0; i < size; ++i) narrow_[i] = rows[i].*member;
            column->WriteBatch(size, nullptr, nullptr, narrow_.data());
        }
    }

    template<bool nullable = false, class Getter>
    void strings(size_t size, Getter get) {
        auto* column = static_cast<parquet::ByteArrayWriter*>(group_->NextColumn());
        bytes_.clear();
        bytes_.reserve(size);
        if constexpr (nullable) levels_.resize(size);
        for (size_t i = 0; i < size; ++i) {
            const std::string_view value = get(i);
            if constexpr (nullable) {
                levels_[i] = value.data() != nullptr;
                if (!levels_[i]) continue;
            }
            bytes_.emplace_back(static_cast<uint32_t>(value.size()), reinterpret_cast<const uint8_t*>(value.data()));
        }
        column->WriteBatch(size, nullable ? levels_.data() : nullptr, nullptr, bytes_.data());
    }
private:
    parquet::RowGroupWriter* group_;
    std::vector<int32_t> narrow_;
    std::vector<int64_t> wide_;
    std::vector<int16_t> levels_;
    std::vector<parquet::ByteArray> bytes_;
};

void write_group(Columns& c, const Inputs& in, const UserDay* rows, size_t size) {
    c.strings(size, [&](size_t i) { return in.person_name(rows[i].person); });
    c.number(rows, size, &UserDay::day);
    c.number(rows, size, &UserDay::sessions);
    c.number(rows, size, &UserDay::events);
    c.number(rows, size, &UserDay::revenue);
    c.strings<true>(size, [&](size_t i) -> std::string_view {
        return rows[i].category == UINT32_MAX ? std::string_view{} : std::string_view(in.categories[rows[i].category]);
    });
}

void write_group(Columns& c, const Inputs& in, const CampaignDay* rows, size_t size) {
    c.strings(size, [&](size_t i) -> std::string_view { return in.campaigns[rows[i].campaign]; });
    c.strings(size, [&](size_t i) -> std::string_view { return in.channels[rows[i].campaign]; });
    c.number(rows, size, &CampaignDay::day);
    c.number(rows, size, &CampaignDay::sessions);
    c.number(rows, size, &CampaignDay::purchasing_sessions);
    c.number(rows, size, &CampaignDay::revenue);
    c.number(rows, size, &CampaignDay::assisted_sessions);
}

template<class Row>
void write_file(const std::filesystem::path& path, const Inputs& in, const std::vector<Row>& rows) {
    auto opened = arrow::io::FileOutputStream::Open(path.string());
    if (!opened.ok()) throw std::runtime_error(opened.status().ToString());
    auto stream = *opened;
    auto properties = parquet::WriterProperties::Builder().compression(parquet::Compression::SNAPPY)->build();
    auto writer = parquet::ParquetFileWriter::Open(stream, schema(std::is_same_v<Row, UserDay>), properties);
    constexpr size_t group_size = 65536;
    for (size_t at = 0; at < rows.size(); at += group_size) {
        Columns columns(writer->AppendRowGroup());
        write_group(columns, in, rows.data() + at, std::min(group_size, rows.size() - at));
    }
    writer->Close();
    auto status = stream->Close();
    if (!status.ok()) throw std::runtime_error(status.ToString());
}
}

void write_output(const std::filesystem::path& root, const Inputs& in, const DailyTables& tables) {
    auto users = root / "user_daily";
    auto campaigns = root / "campaign_daily";
    std::filesystem::create_directories(users);
    std::filesystem::create_directories(campaigns);
    std::vector<const std::vector<UserDay>*> shards;
    for (const auto& rows : tables.users)
        if (!rows.empty()) shards.push_back(&rows);
    const std::vector<UserDay> empty;
    if (shards.empty()) shards.push_back(&empty);
    const size_t parts = shards.size();
    std::vector<std::future<void>> writes;
    auto write_users = [&](size_t part) {
        write_file(users / ("part-" + std::to_string(part) + ".parquet"), in, *shards[part]);
    };
    for (size_t i = 1; i < parts; ++i) writes.push_back(std::async(std::launch::async, write_users, i));
    write_users(0);
    write_file(campaigns / "part-0.parquet", in, tables.campaigns);
    for (auto& write : writes) write.get();
    // A rerun in the same output directory may produce fewer parts.
    for (size_t i = parts; i < 64; ++i) {
        if (!std::filesystem::remove(users / ("part-" + std::to_string(i) + ".parquet"))) break;
    }
}
}
