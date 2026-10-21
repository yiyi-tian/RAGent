#pragma once

#include <boost/json.hpp>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace ragent {

inline constexpr std::size_t header_size = 20;
inline constexpr std::size_t max_body_size = 1024 * 1024;

enum class MessageType : std::uint16_t {
    chat_request = 1,
    rag_request = 2,
    cmd_request = 3,
    response = 100,
    error = 101,
    ping = 200,
    pong = 201
};

struct Frame {
    MessageType type;
    std::uint64_t request_id;
    std::string body;
};

// 仅用于完整帧的线上编码，不依赖任何结构体内存布局。
std::string encode_frame(const Frame& frame);

class Decoder {
public:
    // 调用方应在每次小块追加后取尽完整帧；非法头部抛出异常并关闭连接。
    void append(std::string_view bytes);
    std::optional<Frame> next();
    bool empty() const { return buffer_.empty(); }
private:
    std::string buffer_;
};

// 非法JSON、UTF-8、BOM或非对象消息体均抛出异常。
boost::json::object parse_object(std::string_view body);
bool is_blank(std::string_view text);
Frame make_error(std::uint64_t id, std::string_view code, std::string_view message);
Frame handle_fixed_request(const Frame& request);

} // 命名空间 ragent
