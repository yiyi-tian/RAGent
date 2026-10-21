#include "net/socket.hpp"
#include "protocol/protocol.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <csignal>
#include <deque>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <unordered_map>

namespace {
using namespace ragent;

struct Options {
    std::string host = "127.0.0.1";
    unsigned port = 9000;
    unsigned max_connections = 128;
    unsigned max_pending_bytes = 2 * 1024 * 1024;
};

Options load_config(const std::string& path) {
    std::ifstream file(path);
    if (!file) throw std::invalid_argument("无法读取配置文件: " + path);
    const std::string text{std::istreambuf_iterator<char>(file), {}};
    auto object = parse_object(text);
    Options options;
    for (const auto& field : object) {
        const std::string key(field.key());
        if (key == "host") {
            if (!field.value().is_string()) throw std::invalid_argument("host必须是字符串");
            options.host = std::string(field.value().as_string());
        } else if (key == "port" || key == "max_connections" || key == "max_pending_bytes") {
            if (!field.value().is_int64() && !field.value().is_uint64()) {
                throw std::invalid_argument(key + "必须是整数");
            }
            const unsigned minimum = key == "port" ? 0 : (key == "max_connections" ? 1 : header_size + max_body_size);
            const unsigned maximum = key == "port" ? 65535 : (key == "max_connections" ? 4096 : 64 * 1024 * 1024);
            const auto number = parse_unsigned(boost::json::serialize(field.value()), minimum, maximum);
            if (key == "port") options.port = number;
            if (key == "max_connections") options.max_connections = number;
            if (key == "max_pending_bytes") options.max_pending_bytes = number;
        } else {
            throw std::invalid_argument("未知配置字段: " + key);
        }
    }
    return options;
}

Options parse_options(int argc, char** argv) {
    Options options;
    // 配置文件先载入，显式命令行参数随后覆盖，不依赖参数排列顺序。
    for (int i = 1; i < argc; ++i) {
        if (std::string_view(argv[i]) == "--config") {
            if (++i == argc) throw std::invalid_argument("--config缺少路径");
            options = load_config(argv[i]);
        }
    }
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        if (i + 1 == argc) throw std::invalid_argument("参数缺少值: " + arg);
        const std::string value(argv[++i]);
        if (arg == "--host") options.host = value;
        else if (arg == "--port") options.port = parse_unsigned(value, 0, 65535);
        else if (arg == "--max-connections") options.max_connections = parse_unsigned(value, 1, 4096);
        else if (arg == "--max-pending-bytes") options.max_pending_bytes = parse_unsigned(value, header_size + max_body_size, 64 * 1024 * 1024);
        else if (arg != "--config") throw std::invalid_argument("未知参数: " + arg);
    }
    return options;
}

struct Connection {
    UniqueFd socket;
    Decoder decoder;
    std::uint64_t last_request_id = 0;
    std::deque<std::string> output;
    std::size_t output_offset = 0;
    std::size_t pending_bytes = 0;
};

class Server {
public:
    explicit Server(Options options) : options_(std::move(options)), listener_(make_ipv4_socket()),
        epoll_(::epoll_create1(EPOLL_CLOEXEC)) {
        if (epoll_.get() < 0) throw system_error("epoll_create1");
        int reuse = 1;
        if (::setsockopt(listener_.get(), SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0) {
            throw system_error("setsockopt");
        }
        sockaddr_in address{};
        set_ipv4_address(options_.host, options_.port, address);
        if (::bind(listener_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) throw system_error("bind");
        if (::listen(listener_.get(), 128) < 0) throw system_error("listen");
        socklen_t size = sizeof(address);
        if (::getsockname(listener_.get(), reinterpret_cast<sockaddr*>(&address), &size) < 0) throw system_error("getsockname");
        options_.port = ntohs(address.sin_port);

        // 信号通过epoll处理，避免信号处理函数接触连接或日志对象。
        sigset_t mask;
        ::sigemptyset(&mask);
        ::sigaddset(&mask, SIGINT);
        ::sigaddset(&mask, SIGTERM);
        if (::sigprocmask(SIG_BLOCK, &mask, nullptr) < 0) throw system_error("sigprocmask");
        signals_ = UniqueFd(::signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC));
        if (signals_.get() < 0) throw system_error("signalfd");
        watch(EPOLL_CTL_ADD, listener_.get(), 0, EPOLLIN);
        watch(EPOLL_CTL_ADD, signals_.get(), 1, EPOLLIN);
    }

    void run() {
        std::cout << "Listening on " << options_.host << ':' << options_.port << std::endl;
        std::array<epoll_event, 64> events{};
        while (true) {
            const int count = ::epoll_wait(epoll_.get(), events.data(), events.size(), -1);
            if (count < 0) {
                if (errno == EINTR) continue;
                throw system_error("epoll_wait");
            }
            for (int i = 0; i < count; ++i) {
                const auto id = events[i].data.u64;
                if (id == 1) {
                    std::clog << "[INFO] stage=shutdown connections=" << connections_.size() << '\n';
                    return;
                }
                if (id == 0) {
                    accept_connections();
                    continue;
                }
                auto it = connections_.find(id);
                if (it == connections_.end()) continue;
                try {
                    const auto flags = events[i].events;
                    if (flags & (EPOLLERR | EPOLLHUP | EPOLLRDHUP)) throw std::runtime_error("连接断开");
                    if (flags & EPOLLIN) read_requests(id, it->second);
                    if (flags & EPOLLOUT) write_responses(it->second);
                    watch(EPOLL_CTL_MOD, it->second.socket.get(), id,
                          EPOLLIN | EPOLLRDHUP | (it->second.output.empty() ? 0U : EPOLLOUT));
                } catch (const std::exception& error) {
                    std::clog << "[INFO] conn=" << id << " stage=close reason=" << error.what() << '\n';
                    // close会从epoll移除fd，旧事件通过不复用的ID被忽略。
                    connections_.erase(it);
                }
            }
        }
    }

private:
    void watch(int operation, int fd, std::uint64_t id, std::uint32_t flags) {
        epoll_event event{};
        event.events = flags;
        event.data.u64 = id;
        if (::epoll_ctl(epoll_.get(), operation, fd, &event) < 0) throw system_error("epoll_ctl");
    }

    void accept_connections() {
        // 使用水平触发，每轮限制接入量，防止持续接入饿死已有连接。
        for (unsigned accepted = 0; accepted < 64; ++accepted) {
            UniqueFd socket(::accept4(listener_.get(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
            if (socket.get() < 0) {
                if (errno == EINTR || errno == ECONNABORTED) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) return;
                throw system_error("accept4");
            }
            if (connections_.size() >= options_.max_connections) {
                std::clog << "[INFO] stage=reject reason=max_connections\n";
                continue;
            }
            if (next_connection_id_ == std::numeric_limits<std::uint64_t>::max()) {
                throw std::runtime_error("连接ID耗尽，请重启服务器");
            }
            const auto id = next_connection_id_++;
            watch(EPOLL_CTL_ADD, socket.get(), id, EPOLLIN | EPOLLRDHUP);
            Connection connection;
            connection.socket = std::move(socket);
            connections_.emplace(id, std::move(connection));
            std::clog << "[INFO] conn=" << id << " stage=accept\n";
        }
    }

    void read_requests(std::uint64_t id, Connection& connection) {
        std::array<char, 16384> bytes{};
        for (unsigned reads = 0; reads < 4; ++reads) {
            const auto count = ::recv(connection.socket.get(), bytes.data(), bytes.size(), 0);
            if (count == 0) {
                throw std::runtime_error(connection.decoder.empty() ? "EOF" : "EOF且存在不完整帧");
            }
            if (count < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) return;
                throw system_error("recv");
            }
            connection.decoder.append(std::string_view(bytes.data(), count));
            while (auto frame = connection.decoder.next()) {
                if (frame->request_id == 0 || frame->request_id <= connection.last_request_id) {
                    throw std::runtime_error("request_id为0或不递增");
                }
                connection.last_request_id = frame->request_id;
                auto response = handle_fixed_request(*frame);
                std::clog << "[INFO] conn=" << id << " req=" << frame->request_id
                          << " type=" << static_cast<unsigned>(frame->type) << " stage=fixed_response status="
                          << (response.type == MessageType::error ? "ERROR" : "OK") << '\n';
                auto encoded = encode_frame(response);
                if (connection.pending_bytes + encoded.size() > options_.max_pending_bytes) {
                    throw std::runtime_error("发送缓冲达到上限");
                }
                connection.pending_bytes += encoded.size();
                connection.output.push_back(std::move(encoded));
            }
        }
    }

    void write_responses(Connection& connection) {
        std::size_t budget = 65536;
        while (!connection.output.empty() && budget > 0) {
            const auto& bytes = connection.output.front();
            const auto count = ::send(connection.socket.get(), bytes.data() + connection.output_offset,
                std::min(budget, bytes.size() - connection.output_offset), MSG_NOSIGNAL);
            if (count < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) return;
                throw system_error("send");
            }
            if (count == 0) throw std::runtime_error("send未取得进展");
            connection.output_offset += count;
            connection.pending_bytes -= count;
            budget -= count;
            if (connection.output_offset == bytes.size()) {
                connection.output.pop_front();
                connection.output_offset = 0;
            }
        }
    }

    Options options_;
    UniqueFd listener_;
    UniqueFd epoll_;
    UniqueFd signals_;
    std::unordered_map<std::uint64_t, Connection> connections_;
    std::uint64_t next_connection_id_ = 2;
};
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::cout << "agent-server [--config PATH] [--host IPv4] [--port 0..65535]\n"
                     "             [--max-connections N] [--max-pending-bytes N]\n";
        return 0;
    }
    try {
        Server server(parse_options(argc, argv));
        server.run();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "agent-server: " << error.what() << '\n';
        return 1;
    }
}
