#include "net/socket.hpp"
#include "protocol/protocol.hpp"

#include <array>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <iostream>
#include <limits>
#include <poll.h>
#include <sys/socket.h>

namespace {
using namespace ragent;
using Clock = std::chrono::steady_clock;
volatile std::sig_atomic_t interrupted = 0;

void interrupt_handler(int) { interrupted = 1; }

struct Options {
    std::string host = "127.0.0.1";
    unsigned port = 9000;
    unsigned timeout_ms = 10000;
    std::string mode;
    std::string query;
    bool once = false;
};

MessageType parse_mode(std::string_view mode) {
    if (mode == "chat") return MessageType::chat_request;
    if (mode == "rag") return MessageType::rag_request;
    if (mode == "cmd") return MessageType::cmd_request;
    throw std::invalid_argument("mode必须为chat、rag或cmd");
}

Options parse_options(int argc, char** argv) {
    Options options;
    bool has_query = false;
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        if (i + 1 == argc) throw std::invalid_argument("参数缺少值: " + arg);
        const std::string value(argv[++i]);
        if (arg == "--host") options.host = value;
        else if (arg == "--port") options.port = parse_unsigned(value, 1, 65535);
        else if (arg == "--timeout-ms") options.timeout_ms = parse_unsigned(value, 1, 3600000);
        else if (arg == "--mode") { parse_mode(value); options.mode = value; }
        else if (arg == "--query") { options.query = value; has_query = true; }
        else throw std::invalid_argument("未知参数: " + arg);
    }
    if (has_query != !options.mode.empty()) throw std::invalid_argument("--mode与--query必须同时提供");
    options.once = has_query;
    if (has_query && is_blank(options.query)) throw std::invalid_argument("query不能为空白");
    return options;
}

void wait_socket(int fd, short events, Clock::time_point deadline) {
    while (!interrupted) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (remaining <= 0) throw std::runtime_error("网络等待超时，连接关闭；请求执行结果未知，不自动重试");
        pollfd descriptor{fd, events, 0};
        const int count = ::poll(&descriptor, 1, static_cast<int>(remaining));
        if (count < 0) {
            if (errno == EINTR) continue;
            throw system_error("poll");
        }
        if (count == 0) continue;
        // 先允许调用方消费可读数据，剩余错误交给recv/send或SO_ERROR报告。
        if (descriptor.revents & (events | POLLERR | POLLHUP)) return;
        if (descriptor.revents & POLLNVAL) throw std::runtime_error("Socket已失效");
    }
    throw std::runtime_error("用户中断");
}

class Client {
public:
    explicit Client(const Options& options) : socket_(make_ipv4_socket()), timeout_ms_(options.timeout_ms) {
        sockaddr_in address{};
        set_ipv4_address(options.host, options.port, address);
        const int result = ::connect(socket_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address));
        if (result < 0 && errno != EINPROGRESS) throw system_error("connect");
        if (result < 0) {
            wait_socket(socket_.get(), POLLOUT, deadline());
            int error = 0;
            socklen_t size = sizeof(error);
            if (::getsockopt(socket_.get(), SOL_SOCKET, SO_ERROR, &error, &size) < 0) throw system_error("getsockopt");
            if (error != 0) { errno = error; throw system_error("connect"); }
        }
    }

    bool request(MessageType type, const std::string& query) {
        if (next_id_ == std::numeric_limits<std::uint64_t>::max()) throw std::runtime_error("请求ID耗尽，请重新连接");
        boost::json::object body;
        if (type != MessageType::ping) body["query"] = query;
        const auto id = next_id_++;
        const auto bytes = encode_frame({type, id, boost::json::serialize(body)});
        const auto expires = deadline();
        std::size_t offset = 0;
        while (offset < bytes.size()) {
            wait_socket(socket_.get(), POLLOUT, expires);
            const auto count = ::send(socket_.get(), bytes.data() + offset, bytes.size() - offset, MSG_NOSIGNAL);
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                throw system_error("send");
            }
            if (count == 0) throw std::runtime_error("send未取得进展");
            offset += count;
        }

        std::array<char, 16384> buffer{};
        while (true) {
            if (auto response = decoder_.next()) {
                if (response->request_id != id || !decoder_.empty()) throw std::runtime_error("服务端响应ID或帧数量错误");
                return display(*response, type);
            }
            wait_socket(socket_.get(), POLLIN, expires);
            const auto count = ::recv(socket_.get(), buffer.data(), buffer.size(), 0);
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                throw system_error("recv");
            }
            if (count == 0) throw std::runtime_error("服务端关闭连接或响应不完整");
            decoder_.append(std::string_view(buffer.data(), count));
        }
    }

private:
    Clock::time_point deadline() const { return Clock::now() + std::chrono::milliseconds(timeout_ms_); }

    bool display(const Frame& frame, MessageType request_type) {
        const auto body = parse_object(frame.body);
        if (frame.type == MessageType::error) {
            const auto* code = body.if_contains("code");
            const auto* message = body.if_contains("message");
            if (!code || !code->is_string() || !message || !message->is_string()) throw std::runtime_error("非法错误响应");
            std::cerr << std::string(code->as_string()) << ": " << std::string(message->as_string()) << '\n';
            return false;
        }
        if (request_type == MessageType::ping && frame.type == MessageType::pong) {
            std::cout << "Agent 网络连接正常（未检查依赖服务）。\n";
            return true;
        }
        if (request_type == MessageType::ping || frame.type != MessageType::response) throw std::runtime_error("非法响应类型");
        const auto* text = body.if_contains("text");
        const auto* sources = body.if_contains("sources");
        if (!text || !text->is_string() || !sources || !sources->is_array()) throw std::runtime_error("非法成功响应");
        // 第一阶段只接受空来源；真实RAG接入时再增加来源项展示。
        if (!sources->as_array().empty()) throw std::runtime_error("第一阶段不支持非空sources");
        std::cout << std::string(text->as_string()) << '\n';
        return true;
    }

    UniqueFd socket_;
    unsigned timeout_ms_;
    std::uint64_t next_id_ = 1;
    Decoder decoder_;
};

struct ModeCommand {
    const char* command;
    const char* mode;
    const char* description;
};

constexpr std::array<ModeCommand, 3> mode_commands{{
    {"/chat", "chat", "普通对话"},
    {"/rag", "rag", "知识库问答"},
    {"/cmd", "cmd", "受控操作"},
}};

void help() {
    for (const auto& command : mode_commands) {
        std::cout << command.command << " [问题]  " << command.description << "（第一阶段仅固定响应）\n";
    }
    std::cout << "/back 返回主界面；/help 帮助；/status 网络检查；/clear 清屏；/exit 退出\n"
                 "/list 在CMD模式说明当前操作支持情况。\n";
}

void repl(Client& client, const Options& options) {
    std::string mode;
    std::cout << R"(
     _____            _____             _
    |  __ \     /\   / ____|           | |
    | |__) |   /  \ | |  __  ___ _ __  | |_
    |  _  /   / /\ \| | |_ |/ _ \ '_ \ | __|
    | | \ \  / ____ \ |__| |  __/ | | || |_
    |_|  \_\/_/    \_\_____|\___|_| |_| \__|
    )";

    std::cout << "\nConnected to " << options.host << ':' << options.port
              << "\n输入 /help 查看帮助。\n";
    std::string line;
    while (!interrupted) {
        std::cout << (mode.empty() ? "agent" : mode) << "> " << std::flush;
        if (!std::getline(std::cin, line)) break;
        if (is_blank(line)) continue;
        const auto start = line.find_first_not_of(" \t\r\n");
        const auto end = line.find_last_not_of(" \t\r\n");
        const auto input = line.substr(start, end - start + 1);
        if (input.front() != '/') {
            if (mode.empty()) std::cout << "请先选择 /chat、/rag 或 /cmd 模式。\n";
            else client.request(parse_mode(mode), line);
            continue;
        }
        const auto separator = input.find_first_of(" \t");
        const auto command = input.substr(0, separator);
        const auto argument_start = separator == std::string::npos ? std::string::npos : input.find_first_not_of(" \t", separator);
        const auto argument = argument_start == std::string::npos ? std::string{} : input.substr(argument_start);
        bool handled = false;
        for (const auto& definition : mode_commands) {
            if (command != definition.command) continue;
            if (argument.empty()) mode = definition.mode;
            else client.request(parse_mode(definition.mode), argument);
            handled = true;
            break;
        }
        if (handled) continue;
        if (!argument.empty()) { std::cout << "此命令不接受参数。\n"; continue; }
        if (command == "/exit") break;
        if (command == "/back") mode.clear();
        else if (command == "/help" || command == "/") help();
        else if (command == "/clear") std::cout << "\033[2J\033[H";
        else if (command == "/status") client.request(MessageType::ping, "");
        else if (command == "/list") std::cout << (mode == "cmd" ? "第一阶段不执行命令，操作列表将在命令执行阶段提供。\n" : "请先进入 /cmd 模式。\n");
        else std::cout << "未知命令，输入 /help 查看帮助。\n";
    }
}
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::cout << "agent [--host IPv4] [--port PORT] [--timeout-ms N]\n"
                     "      [--mode chat|rag|cmd --query TEXT]\n";
        return 0;
    }
    struct sigaction action{};
    action.sa_handler = interrupt_handler;
    ::sigemptyset(&action.sa_mask);
    if (::sigaction(SIGINT, &action, nullptr) < 0) {
        std::cerr << system_error("sigaction").what() << '\n';
        return 1;
    }
    try {
        const auto options = parse_options(argc, argv);
        Client client(options);
        if (options.once) return client.request(parse_mode(options.mode), options.query) ? 0 : 3;
        repl(client, options);
        return 0;
    } catch (const std::invalid_argument& error) {
        std::cerr << "agent: " << error.what() << '\n';
        return 2;
    } catch (const std::exception& error) {
        if (interrupted) return 0;
        std::cerr << "agent: " << error.what() << '\n';
        return 1;
    }
}
