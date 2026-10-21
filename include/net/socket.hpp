#pragma once

#include <netinet/in.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace ragent {

class UniqueFd {
public:
    explicit UniqueFd(int fd = -1) : fd_(fd) {}
    ~UniqueFd();
    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;
    UniqueFd(UniqueFd&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    UniqueFd& operator=(UniqueFd&& other) noexcept;
    int get() const { return fd_; }
private:
    int fd_;
};

std::runtime_error system_error(std::string_view operation);
unsigned parse_unsigned(std::string_view value, unsigned minimum, unsigned maximum);
// 返回非阻塞、执行新程序时不继承的Socket；connect可能需要等待完成。
UniqueFd make_ipv4_socket();
void set_ipv4_address(const std::string& host, unsigned port, struct sockaddr_in& address);

} // 命名空间 ragent
