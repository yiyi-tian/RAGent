#define _POSIX_C_SOURCE 200809L

#include "net/socket.h"
#include "protocol/protocol.h"

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

typedef enum {
    CLIENT_MODE_AGENT,
    CLIENT_MODE_CHAT,
    CLIENT_MODE_RAG,
    CLIENT_MODE_CMD
} ClientMode;

typedef struct {
    char host[INET_ADDRSTRLEN];
    unsigned port;
    unsigned timeout_ms;
    ClientMode mode;
    const char *query;
    bool once;
} Options;

typedef struct {
    int socket_fd;
    unsigned timeout_ms;
    uint64_t next_request_id;
    FrameDecoder decoder;
} Client;

static volatile sig_atomic_t interrupted = 0;

static void interrupt_handler(int signal_number)
{
    (void)signal_number;
    interrupted = 1;
}

static const char *mode_name(ClientMode mode)
{
    switch (mode) {
    case CLIENT_MODE_AGENT: return "agent";
    case CLIENT_MODE_CHAT: return "chat";
    case CLIENT_MODE_RAG: return "rag";
    case CLIENT_MODE_CMD: return "cmd";
    }
    return "agent";
}

static MessageType mode_message_type(ClientMode mode)
{
    switch (mode) {
    case CLIENT_MODE_AGENT: return MESSAGE_AGENT_REQUEST;
    case CLIENT_MODE_CHAT: return MESSAGE_CHAT_REQUEST;
    case CLIENT_MODE_RAG: return MESSAGE_RAG_REQUEST;
    case CLIENT_MODE_CMD: return MESSAGE_CMD_REQUEST;
    }
    return MESSAGE_AGENT_REQUEST;
}

static int parse_mode(const char *value, ClientMode *mode)
{
    if (strcmp(value, "agent") == 0) *mode = CLIENT_MODE_AGENT;
    else if (strcmp(value, "chat") == 0) *mode = CLIENT_MODE_CHAT;
    else if (strcmp(value, "rag") == 0) *mode = CLIENT_MODE_RAG;
    else if (strcmp(value, "cmd") == 0) *mode = CLIENT_MODE_CMD;
    else return -1;
    return 0;
}

static int parse_options(int argc, char **argv, Options *options)
{
    memset(options, 0, sizeof(*options));
    strcpy(options->host, "127.0.0.1");
    options->port = 9000;
    options->timeout_ms = 10000;
    options->mode = CLIENT_MODE_AGENT;
    bool has_mode = false;
    bool has_query = false;
    for (int index = 1; index < argc; ++index) {
        const char *argument = argv[index];
        if (index + 1 >= argc) {
            fprintf(stderr, "参数缺少值: %s\n", argument);
            return -1;
        }
        const char *value = argv[++index];
        unsigned parsed = 0;
        if (strcmp(argument, "--host") == 0) {
            struct sockaddr_in address;
            if (strlen(value) >= sizeof(options->host) || set_ipv4_address(value, 0, &address) != 0) {
                fprintf(stderr, "host必须是IPv4地址\n");
                return -1;
            }
            strcpy(options->host, value);
        } else if (strcmp(argument, "--port") == 0) {
            if (parse_unsigned(value, 1, 65535, &parsed) != 0) goto invalid_number;
            options->port = parsed;
        } else if (strcmp(argument, "--timeout-ms") == 0) {
            if (parse_unsigned(value, 1, 3600000, &parsed) != 0) goto invalid_number;
            options->timeout_ms = parsed;
        } else if (strcmp(argument, "--mode") == 0) {
            if (parse_mode(value, &options->mode) != 0) {
                fprintf(stderr, "mode必须为agent、chat、rag或cmd\n");
                return -1;
            }
            has_mode = true;
        } else if (strcmp(argument, "--query") == 0) {
            options->query = value;
            has_query = true;
        } else {
            fprintf(stderr, "未知参数: %s\n", argument);
            return -1;
        }
        continue;
invalid_number:
        fprintf(stderr, "数值参数格式错误或超出范围: %s\n", argument);
        return -1;
    }
    if (has_mode != has_query) {
        fprintf(stderr, "--mode与--query必须同时提供\n");
        return -1;
    }
    options->once = has_query;
    if (options->once && text_is_blank(options->query, strlen(options->query))) {
        fprintf(stderr, "query不能为空白\n");
        return -1;
    }
    return 0;
}

static int wait_socket(int fd, short events, int64_t deadline_ms)
{
    while (!interrupted) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
        const int64_t current_ms = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
        const int64_t remaining = deadline_ms - current_ms;
        if (remaining <= 0) {
            fprintf(stderr, "网络等待超时，连接关闭；请求执行结果未知，不自动重试\n");
            errno = ETIMEDOUT;
            return -1;
        }
        struct pollfd descriptor = {.fd = fd, .events = events, .revents = 0};
        const int count = poll(&descriptor, 1, remaining > INT_MAX ? INT_MAX : (int)remaining);
        if (count < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (count == 0) continue;
        if ((descriptor.revents & (events | POLLERR | POLLHUP)) != 0) return 0;
        if ((descriptor.revents & POLLNVAL) != 0) {
            errno = EBADF;
            return -1;
        }
    }
    errno = EINTR;
    return -1;
}

static int64_t deadline_after(unsigned timeout_ms)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 + timeout_ms;
}

static int client_init(Client *client, const Options *options)
{
    memset(client, 0, sizeof(*client));
    client->socket_fd = make_ipv4_socket();
    client->timeout_ms = options->timeout_ms;
    client->next_request_id = 1;
    frame_decoder_init(&client->decoder);
    if (client->socket_fd < 0) return -1;
    struct sockaddr_in address;
    if (set_ipv4_address(options->host, options->port, &address) != 0) goto failure;
    const int result = connect(client->socket_fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) goto failure;
    if (result < 0) {
        if (wait_socket(client->socket_fd, POLLOUT, deadline_after(client->timeout_ms)) != 0) goto failure;
        int socket_error = 0;
        socklen_t error_size = sizeof(socket_error);
        if (getsockopt(client->socket_fd, SOL_SOCKET, SO_ERROR, &socket_error, &error_size) != 0) goto failure;
        if (socket_error != 0) {
            errno = socket_error;
            goto failure;
        }
    }
    return 0;

failure: {
        const int saved_errno = errno;
        close(client->socket_fd);
        client->socket_fd = -1;
        frame_decoder_destroy(&client->decoder);
        errno = saved_errno;
        return -1;
    }
}

static void client_destroy(Client *client)
{
    if (client->socket_fd >= 0) close(client->socket_fd);
    frame_decoder_destroy(&client->decoder);
    client->socket_fd = -1;
}

static int display_response(const Frame *frame, MessageType request_type)
{
    json_object *body = json_parse_object(frame->body, frame->body_length);
    if (body == NULL) {
        fprintf(stderr, "服务端返回非法JSON响应\n");
        return -1;
    }
    if (frame->type == MESSAGE_ERROR) {
        json_object *code = NULL;
        json_object *message = NULL;
        if (!json_object_object_get_ex(body, "code", &code) ||
            !json_object_is_type(code, json_type_string) ||
            !json_object_object_get_ex(body, "message", &message) ||
            !json_object_is_type(message, json_type_string)) {
            json_object_put(body);
            fprintf(stderr, "非法错误响应\n");
            return -1;
        }
        fprintf(stderr, "%s: %s\n", json_object_get_string(code), json_object_get_string(message));
        json_object_put(body);
        return 1;
    }
    if (request_type == MESSAGE_PING && frame->type == MESSAGE_PONG) {
        puts("Agent 网络连接正常（未检查依赖服务）。");
        json_object_put(body);
        return 0;
    }
    if (request_type == MESSAGE_PING || frame->type != MESSAGE_RESPONSE) {
        json_object_put(body);
        fprintf(stderr, "非法响应类型\n");
        return -1;
    }
    json_object *text = NULL;
    json_object *sources = NULL;
    if (!json_object_object_get_ex(body, "text", &text) ||
        !json_object_is_type(text, json_type_string) ||
        !json_object_object_get_ex(body, "sources", &sources) ||
        !json_object_is_type(sources, json_type_array)) {
        json_object_put(body);
        fprintf(stderr, "非法成功响应\n");
        return -1;
    }
    if (json_object_array_length(sources) != 0) {
        json_object_put(body);
        fprintf(stderr, "第一阶段不支持非空sources\n");
        return -1;
    }
    puts(json_object_get_string(text));
    json_object_put(body);
    return 0;
}

static int client_request(Client *client, MessageType type, const char *query)
{
    if (client->next_request_id == UINT64_MAX) {
        fprintf(stderr, "请求ID耗尽，请重新连接\n");
        return -1;
    }
    json_object *body = json_object_new_object();
    if (body == NULL) return -1;
    if (type != MESSAGE_PING) {
        json_object_object_add(body, "query", json_object_new_string(query));
    }
    const char *json = json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN);
    Frame request = {.type = type, .request_id = client->next_request_id++,
                     .body = (unsigned char *)json, .body_length = strlen(json)};
    unsigned char *encoded = NULL;
    size_t encoded_length = 0;
    if (frame_encode(&request, &encoded, &encoded_length) != 0) {
        json_object_put(body);
        fprintf(stderr, "无法编码请求帧\n");
        return -1;
    }
    const int64_t deadline = deadline_after(client->timeout_ms);
    size_t offset = 0;
    while (offset < encoded_length) {
        if (wait_socket(client->socket_fd, POLLOUT, deadline) != 0) goto io_error;
        const ssize_t sent = send(client->socket_fd, encoded + offset,
                                  encoded_length - offset, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            goto io_error;
        }
        if (sent == 0) {
            errno = EPIPE;
            goto io_error;
        }
        offset += (size_t)sent;
    }
    free(encoded);
    json_object_put(body);

    unsigned char buffer[16384];
    while (true) {
        Frame response = {0};
        const char *decode_error = NULL;
        const int decoded = frame_decoder_next(&client->decoder, &response, &decode_error);
        if (decoded < 0) {
            fprintf(stderr, "服务端响应帧非法: %s\n", decode_error == NULL ? "未知错误" : decode_error);
            return -1;
        }
        if (decoded > 0) {
            if (response.request_id != request.request_id || !frame_decoder_empty(&client->decoder)) {
                frame_destroy(&response);
                fprintf(stderr, "服务端响应ID或帧数量错误\n");
                return -1;
            }
            const int displayed = display_response(&response, type);
            frame_destroy(&response);
            return displayed;
        }
        if (wait_socket(client->socket_fd, POLLIN, deadline) != 0) return -1;
        const ssize_t received = recv(client->socket_fd, buffer, sizeof(buffer), 0);
        if (received < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        if (received == 0) {
            fprintf(stderr, "服务端关闭连接或响应不完整\n");
            return -1;
        }
        if (frame_decoder_append(&client->decoder, buffer, (size_t)received) != 0) {
            fprintf(stderr, "服务端响应超过接收缓冲上限\n");
            return -1;
        }
    }

io_error:
    free(encoded);
    json_object_put(body);
    print_system_error("网络请求");
    return -1;
}

static void print_help(void)
{
    puts("/agent [问题]  Agent自动选择可用工具（第一阶段仅固定响应）\n"
         "/chat [问题]   普通对话模式（第一阶段仅固定响应）\n"
         "/rag [问题]    知识库问答模式（第一阶段仅固定响应）\n"
         "/cmd [问题]    受控操作模式（第一阶段仅固定响应）\n"
         "/back 返回Agent模式；/help 帮助；/status 网络检查；/clear 清屏；/exit 退出\n"
         "/list 在CMD模式说明当前操作支持情况。");
}

static int repl(Client *client, const Options *options)
{
    puts("\nConnected to Agent");
    printf("连接地址 %s:%u，默认使用 Agent 模式；输入 /help 查看帮助。\n",
           options->host, options->port);
    ClientMode mode = CLIENT_MODE_AGENT;
    char *line = NULL;
    size_t capacity = 0;
    while (!interrupted) {
        printf("%s> ", mode_name(mode));
        fflush(stdout);
        const ssize_t length = getline(&line, &capacity, stdin);
        if (length < 0) break;
        size_t input_start = 0;
        size_t input_end = (size_t)length;
        while (input_start < input_end && strchr(" \t\r\n", line[input_start]) != NULL) ++input_start;
        while (input_end > input_start && strchr(" \t\r\n", line[input_end - 1]) != NULL) --input_end;
        if (input_start == input_end) continue;
        if (line[input_start] != '/') {
            char *query = strndup(line, (size_t)length - (line[(size_t)length - 1] == '\n' ? 1U : 0U));
            if (query == NULL) break;
            const int result = client_request(client, mode_message_type(mode), query);
            free(query);
            if (result < 0) {
                free(line);
                return -1;
            }
            continue;
        }

        const size_t command_start = input_start;
        size_t command_end = command_start;
        while (command_end < input_end && line[command_end] != ' ' && line[command_end] != '\t') ++command_end;
        size_t argument_start = command_end;
        while (argument_start < input_end && (line[argument_start] == ' ' || line[argument_start] == '\t')) ++argument_start;
        const char *argument = line + argument_start;
        const size_t argument_length = input_end - argument_start;
        const size_t command_length = command_end - command_start;
        struct {
            const char *name;
            ClientMode mode;
        } commands[] = {{"/agent", CLIENT_MODE_AGENT}, {"/chat", CLIENT_MODE_CHAT},
                        {"/rag", CLIENT_MODE_RAG}, {"/cmd", CLIENT_MODE_CMD}};
        bool mode_command = false;
        for (size_t index = 0; index < sizeof(commands) / sizeof(commands[0]); ++index) {
            if (strlen(commands[index].name) != command_length ||
                memcmp(line + command_start, commands[index].name, command_length) != 0) continue;
            mode_command = true;
            if (argument_length == 0) {
                mode = commands[index].mode;
            } else {
                char *query = strndup(argument, argument_length);
                if (query == NULL) {
                    free(line);
                    return -1;
                }
                const int result = client_request(client, mode_message_type(commands[index].mode), query);
                free(query);
                if (result < 0) {
                    free(line);
                    return -1;
                }
            }
            break;
        }
        if (mode_command) continue;
        if (argument_length > 0) {
            puts("此命令不接受参数。");
            continue;
        }
        const size_t command_length_exit = strlen("/exit");
        if (command_length == command_length_exit && memcmp(line + command_start, "/exit", command_length) == 0) break;
        if (command_length == strlen("/back") && memcmp(line + command_start, "/back", command_length) == 0) {
            mode = CLIENT_MODE_AGENT;
        } else if ((command_length == strlen("/help") && memcmp(line + command_start, "/help", command_length) == 0) ||
                   (command_length == 1 && line[command_start] == '/')) {
            print_help();
        } else if (command_length == strlen("/clear") && memcmp(line + command_start, "/clear", command_length) == 0) {
            fputs("\033[2J\033[H", stdout);
        } else if (command_length == strlen("/status") && memcmp(line + command_start, "/status", command_length) == 0) {
            if (client_request(client, MESSAGE_PING, "") < 0) {
                free(line);
                return -1;
            }
        } else if (command_length == strlen("/list") && memcmp(line + command_start, "/list", command_length) == 0) {
            puts(mode == CLIENT_MODE_CMD ? "第一阶段不执行命令，操作列表将在命令执行阶段提供。" : "请先进入 /cmd 模式。");
        } else {
            puts("未知命令，输入 /help 查看帮助。");
        }
    }
    free(line);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        puts("agent [--host IPv4] [--port PORT] [--timeout-ms N]\n"
             "      [--mode agent|chat|rag|cmd --query TEXT]");
        return 0;
    }
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = interrupt_handler;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) != 0) {
        print_system_error("sigaction");
        return 1;
    }
    Options options;
    if (parse_options(argc, argv, &options) != 0) {
        return 2;
    }
    Client client;
    if (client_init(&client, &options) != 0) {
        print_system_error("连接Agent失败");
        client_destroy(&client);
        return 1;
    }
    int result;
    if (options.once) {
        result = client_request(&client, mode_message_type(options.mode), options.query);
        result = result == 0 ? 0 : (result > 0 ? 3 : 1);
    } else {
        result = repl(&client, &options) == 0 ? 0 : 1;
    }
    client_destroy(&client);
    if (interrupted && !options.once) return 0;
    return result;
}
