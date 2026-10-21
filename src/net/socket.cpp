#include "net/socket.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <cstdint>
#include <sys/socket.h>
#include <unistd.h>

namespace ragent {
UniqueFd::~UniqueFd() {
    // Linux中close被EINTR打断后也不能重试，fd可能已被释放和复用。
    if (fd_ >= 0) ::close(fd_);
}

UniqueFd& UniqueFd::operator=(UniqueFd&& other) noexcept {
    if (this != &other) {
        if (fd_ >= 0) ::close(fd_);
        fd_ = std::exchange(other.fd_, -1);
    }
    return *this;
}

std::runtime_error system_error(std::string_view operation) {
    const int saved_errno = errno;
    return std::runtime_error(std::string(operation) + ": " + std::strerror(saved_errno));
}

unsigned parse_unsigned(std::string_view value, unsigned minimum, unsigned maximum) {
    unsigned result = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() ||
        result < minimum || result > maximum) {
        throw std::invalid_argument("数值参数超出范围或格式错误: " + std::string(value));
    }
    return result;
}

UniqueFd make_ipv4_socket() {
    const int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) throw system_error("socket");
    return UniqueFd(fd);
}

void set_ipv4_address(const std::string& host, unsigned port, sockaddr_in& address) {
    address = {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<std::uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &address.sin_addr) != 1) {
        throw std::invalid_argument("host必须为IPv4地址: " + host);
    }
}
} // 命名空间 ragent
