#include "protocol/protocol.hpp"

#include <stdexcept>

namespace ragent {
namespace {
constexpr std::uint32_t magic = 0x52414754;

void write_integer(std::string& bytes, std::uint64_t value, unsigned width) {
    for (unsigned remaining = width; remaining > 0; --remaining) {
        bytes.push_back(static_cast<char>((value >> ((remaining - 1) * 8)) & 0xff));
    }
}

std::uint64_t read_integer(std::string_view bytes, std::size_t offset, unsigned width) {
    std::uint64_t value = 0;
    for (unsigned i = 0; i < width; ++i) {
        value = (value << 8) | static_cast<unsigned char>(bytes[offset + i]);
    }
    return value;
}
}

std::string encode_frame(const Frame& frame) {
    if (frame.body.size() > max_body_size) {
        throw std::runtime_error("消息体超过1 MiB");
    }
    std::string bytes;
    bytes.reserve(header_size + frame.body.size());
    write_integer(bytes, magic, 4);
    write_integer(bytes, 1, 2);
    write_integer(bytes, static_cast<std::uint16_t>(frame.type), 2);
    write_integer(bytes, frame.body.size(), 4);
    write_integer(bytes, frame.request_id, 8);
    bytes += frame.body;
    return bytes;
}

void Decoder::append(std::string_view bytes) {
    // 为单次读取保留余量，多个合法帧须逐个消费，不累计到无限大。
    if (bytes.size() > 16384 || buffer_.size() + bytes.size() > max_body_size + header_size + 16384) {
        throw std::runtime_error("接收缓冲达到上限");
    }
    buffer_.append(bytes.data(), bytes.size());
}

std::optional<Frame> Decoder::next() {
    if (buffer_.size() < header_size) return std::nullopt;
    if (read_integer(buffer_, 0, 4) != magic) throw std::runtime_error("magic错误");
    if (read_integer(buffer_, 4, 2) != 1) throw std::runtime_error("version不支持");
    const auto length = read_integer(buffer_, 8, 4);
    if (length > max_body_size) throw std::runtime_error("消息体长度超限");
    if (buffer_.size() < header_size + length) return std::nullopt;
    Frame frame{
        static_cast<MessageType>(read_integer(buffer_, 6, 2)),
        read_integer(buffer_, 12, 8),
        buffer_.substr(header_size, length),
    };
    buffer_.erase(0, header_size + length);
    return frame;
}

boost::json::object parse_object(std::string_view body) {
    if (body.substr(0, 3) == "\xef\xbb\xbf") throw std::runtime_error("不允许JSON BOM");
    boost::system::error_code ec;
    auto value = boost::json::parse(boost::json::string_view(body.data(), body.size()), ec);
    if (ec || !value.is_object()) throw std::runtime_error("消息体必须是合法UTF-8 JSON对象");
    return std::move(value.as_object());
}

bool is_blank(std::string_view text) {
    return text.find_first_not_of(" \t\r\n\v\f") == std::string_view::npos;
}

Frame make_error(std::uint64_t id, std::string_view code, std::string_view message) {
    boost::json::object body;
    body["code"] = boost::json::string_view(code.data(), code.size());
    body["message"] = boost::json::string_view(message.data(), message.size());
    return {MessageType::error, id, boost::json::serialize(body)};
}

Frame handle_fixed_request(const Frame& request) {
    const auto type = request.type;
    if (type != MessageType::chat_request && type != MessageType::rag_request &&
        type != MessageType::cmd_request && type != MessageType::ping) {
        return make_error(request.request_id, "UNSUPPORTED_MESSAGE_TYPE", "消息类型未知或方向不合法");
    }
    boost::json::object body;
    try {
        body = parse_object(request.body);
    } catch (const std::runtime_error&) {
        return make_error(request.request_id, "INVALID_REQUEST", "消息体必须是合法UTF-8 JSON对象");
    }
    if (type == MessageType::ping) return {MessageType::pong, request.request_id, "{}"};
    const auto* query = body.if_contains("query");
    if (!query || !query->is_string() || is_blank(std::string_view(query->as_string().data(), query->as_string().size()))) {
        return make_error(request.request_id, "INVALID_REQUEST", "query必须是非空白字符串");
    }
    boost::json::object result;
    result["text"] = "server received: " + std::string(query->as_string());
    result["sources"] = boost::json::array{};
    auto encoded = boost::json::serialize(result);
    if (encoded.size() > max_body_size) {
        return make_error(request.request_id, "INTERNAL_ERROR", "编码后的响应超过消息体上限");
    }
    return {MessageType::response, request.request_id, std::move(encoded)};
}
} // 命名空间 ragent
