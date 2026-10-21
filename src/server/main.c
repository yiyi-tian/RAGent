#define _GNU_SOURCE

#include "net/socket.h"
#include "protocol/protocol.h"

#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <unistd.h>

typedef struct {
    char host[INET_ADDRSTRLEN];
    unsigned port;
    unsigned max_connections;
    unsigned max_pending_bytes;
} Options;

typedef struct OutputChunk {
    unsigned char *data;
    size_t length;
    struct OutputChunk *next;
} OutputChunk;

typedef struct {
    int socket_fd;
    uint64_t id;
    uint64_t last_request_id;
    FrameDecoder decoder;
    OutputChunk *output_head;
    OutputChunk *output_tail;
    size_t output_offset;
    size_t pending_bytes;
} Connection;

typedef struct {
    Options options;
    int listener_fd;
    int epoll_fd;
    int signal_fd;
    Connection *connections;
    size_t connection_count;
    uint64_t next_connection_id;
} Server;

static void set_error(char *buffer, size_t capacity, const char *message)
{
    if (capacity > 0) {
        snprintf(buffer, capacity, "%s", message);
    }
}

static Options default_options(void)
{
    Options options = {.host = "127.0.0.1", .port = 9000,
                       .max_connections = 128, .max_pending_bytes = 2U * 1024U * 1024U};
    return options;
}

static int parse_config_number(json_object *value, const char *key, unsigned minimum,
                               unsigned maximum, unsigned *result, char *error, size_t error_size)
{
    if (!json_object_is_type(value, json_type_int)) {
        snprintf(error, error_size, "%s必须是整数", key);
        return -1;
    }
    const int64_t integer = json_object_get_int64(value);
    if (integer < 0 || (uint64_t)integer > UINT_MAX ||
        parse_unsigned(json_object_to_json_string_ext(value, JSON_C_TO_STRING_PLAIN),
                       minimum, maximum, result) != 0) {
        snprintf(error, error_size, "%s超出范围", key);
        return -1;
    }
    return 0;
}

static int load_config(const char *path, Options *options, char *error, size_t error_size)
{
    FILE *file = fopen(path, "rb");
    if (file == NULL) {
        snprintf(error, error_size, "无法读取配置文件: %s", path);
        return -1;
    }
    if (fseek(file, 0, SEEK_END) != 0) {
        fclose(file);
        set_error(error, error_size, "无法读取配置文件长度");
        return -1;
    }
    const long file_length = ftell(file);
    if (file_length < 0 || file_length > (long)RAGENT_MAX_BODY_SIZE || fseek(file, 0, SEEK_SET) != 0) {
        fclose(file);
        set_error(error, error_size, "配置文件大小非法");
        return -1;
    }
    unsigned char *text = malloc((size_t)file_length + 1U);
    if (text == NULL) {
        fclose(file);
        set_error(error, error_size, "无法分配配置文件缓冲区");
        return -1;
    }
    const size_t count = fread(text, 1, (size_t)file_length, file);
    const int read_error = ferror(file);
    fclose(file);
    if (read_error || count != (size_t)file_length) {
        free(text);
        set_error(error, error_size, "读取配置文件失败");
        return -1;
    }
    text[count] = '\0';
    json_object *root = json_parse_object(text, count);
    free(text);
    if (root == NULL) {
        set_error(error, error_size, "配置文件必须是合法JSON对象");
        return -1;
    }

    json_object_object_foreach(root, key, value) {
        if (strcmp(key, "host") == 0) {
            if (!json_object_is_type(value, json_type_string)) {
                set_error(error, error_size, "host必须是字符串");
                json_object_put(root);
                return -1;
            }
            const char *host = json_object_get_string(value);
            if (strlen(host) >= sizeof(options->host)) {
                set_error(error, error_size, "host长度超限");
                json_object_put(root);
                return -1;
            }
            strcpy(options->host, host);
        } else if (strcmp(key, "port") == 0) {
            if (parse_config_number(value, key, 0, 65535, &options->port, error, error_size) != 0) {
                json_object_put(root);
                return -1;
            }
        } else if (strcmp(key, "max_connections") == 0) {
            if (parse_config_number(value, key, 1, 4096, &options->max_connections, error, error_size) != 0) {
                json_object_put(root);
                return -1;
            }
        } else if (strcmp(key, "max_pending_bytes") == 0) {
            if (parse_config_number(value, key, RAGENT_HEADER_SIZE + RAGENT_MAX_BODY_SIZE,
                                   64U * 1024U * 1024U, &options->max_pending_bytes,
                                   error, error_size) != 0) {
                json_object_put(root);
                return -1;
            }
        } else {
            snprintf(error, error_size, "未知配置字段: %s", key);
            json_object_put(root);
            return -1;
        }
    }
    json_object_put(root);
    return 0;
}

static int parse_options(int argc, char **argv, Options *options, char *error, size_t error_size)
{
    *options = default_options();
    for (int index = 1; index < argc; ++index) {
        if (strcmp(argv[index], "--config") == 0) {
            if (index + 1 >= argc) {
                set_error(error, error_size, "--config缺少路径");
                return -1;
            }
            if (load_config(argv[++index], options, error, error_size) != 0) {
                return -1;
            }
        }
    }
    for (int index = 1; index < argc; ++index) {
        const char *argument = argv[index];
        if (index + 1 >= argc) {
            snprintf(error, error_size, "参数缺少值: %s", argument);
            return -1;
        }
        const char *value = argv[++index];
        unsigned parsed = 0;
        if (strcmp(argument, "--config") == 0) {
            continue;
        } else if (strcmp(argument, "--host") == 0) {
            struct sockaddr_in address;
            if (strlen(value) >= sizeof(options->host) ||
                set_ipv4_address(value, 0, &address) != 0) {
                set_error(error, error_size, "host必须是IPv4地址");
                return -1;
            }
            strcpy(options->host, value);
        } else if (strcmp(argument, "--port") == 0) {
            if (parse_unsigned(value, 0, 65535, &parsed) != 0) goto invalid_number;
            options->port = parsed;
        } else if (strcmp(argument, "--max-connections") == 0) {
            if (parse_unsigned(value, 1, 4096, &parsed) != 0) goto invalid_number;
            options->max_connections = parsed;
        } else if (strcmp(argument, "--max-pending-bytes") == 0) {
            if (parse_unsigned(value, RAGENT_HEADER_SIZE + RAGENT_MAX_BODY_SIZE,
                               64U * 1024U * 1024U, &parsed) != 0) goto invalid_number;
            options->max_pending_bytes = parsed;
        } else {
            snprintf(error, error_size, "未知参数: %s", argument);
            return -1;
        }
        continue;
invalid_number:
        snprintf(error, error_size, "数值参数格式错误或超出范围: %s", argument);
        return -1;
    }
    return 0;
}

static int watch_fd(Server *server, int operation, int fd, uint64_t id, uint32_t flags)
{
    struct epoll_event event = {.events = flags, .data.u64 = id};
    return epoll_ctl(server->epoll_fd, operation, fd, &event);
}

static void output_chunks_destroy(OutputChunk *chunk)
{
    while (chunk != NULL) {
        OutputChunk *next = chunk->next;
        free(chunk->data);
        free(chunk);
        chunk = next;
    }
}

static void connection_destroy(Connection *connection)
{
    if (connection->socket_fd >= 0) close(connection->socket_fd);
    frame_decoder_destroy(&connection->decoder);
    output_chunks_destroy(connection->output_head);
    memset(connection, 0, sizeof(*connection));
    connection->socket_fd = -1;
}

static void remove_connection(Server *server, size_t index)
{
    connection_destroy(&server->connections[index]);
    --server->connection_count;
    if (index != server->connection_count) {
        server->connections[index] = server->connections[server->connection_count];
        memset(&server->connections[server->connection_count], 0,
               sizeof(server->connections[server->connection_count]));
        server->connections[server->connection_count].socket_fd = -1;
    }
}

static size_t find_connection(const Server *server, uint64_t id)
{
    for (size_t index = 0; index < server->connection_count; ++index) {
        if (server->connections[index].id == id) return index;
    }
    return server->connection_count;
}

static int queue_frame(Server *server, Connection *connection, Frame *frame)
{
    OutputChunk *chunk = calloc(1, sizeof(*chunk));
    if (chunk == NULL || frame_encode(frame, &chunk->data, &chunk->length) != 0) {
        free(chunk);
        return -1;
    }
    if (chunk->length > server->options.max_pending_bytes - connection->pending_bytes) {
        free(chunk->data);
        free(chunk);
        errno = ENOBUFS;
        return -1;
    }
    if (connection->output_tail == NULL) connection->output_head = chunk;
    else connection->output_tail->next = chunk;
    connection->output_tail = chunk;
    connection->pending_bytes += chunk->length;
    return 0;
}

static int process_frame(Server *server, uint64_t connection_id, Connection *connection,
                         Frame *request)
{
    if (request->request_id == 0 || request->request_id <= connection->last_request_id) {
        errno = EPROTO;
        return -1;
    }
    connection->last_request_id = request->request_id;
    Frame response = {0};
    if (handle_fixed_request(request, &response) != 0) {
        errno = ENOMEM;
        return -1;
    }
    fprintf(stderr, "[INFO] conn=%" PRIu64 " req=%" PRIu64
                    " type=%u stage=fixed_response status=%s\n",
            connection_id, request->request_id, (unsigned)request->type,
            response.type == MESSAGE_ERROR ? "ERROR" : "OK");
    const int queued = queue_frame(server, connection, &response);
    frame_destroy(&response);
    if (queued != 0) return -1;
    return 0;
}

static int read_requests(Server *server, uint64_t connection_id, Connection *connection)
{
    unsigned char bytes[16384];
    for (unsigned read_count = 0; read_count < 4; ++read_count) {
        const ssize_t count = recv(connection->socket_fd, bytes, sizeof(bytes), 0);
        if (count == 0) {
            errno = ECONNRESET;
            return -1;
        }
        if (count < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;
            return -1;
        }
        if (frame_decoder_append(&connection->decoder, bytes, (size_t)count) != 0) {
            errno = ENOBUFS;
            return -1;
        }
        while (true) {
            Frame request = {0};
            const char *decode_error = NULL;
            const int decoded = frame_decoder_next(&connection->decoder, &request, &decode_error);
            if (decoded == 0) break;
            if (decoded < 0) {
                errno = EPROTO;
                return -1;
            }
            const int processed = process_frame(server, connection_id, connection, &request);
            frame_destroy(&request);
            if (processed != 0) return -1;
        }
    }
    return 0;
}

static int write_responses(Connection *connection)
{
    size_t budget = 65536U;
    while (connection->output_head != NULL && budget > 0) {
        OutputChunk *chunk = connection->output_head;
        const size_t remaining = chunk->length - connection->output_offset;
        const size_t to_write = remaining < budget ? remaining : budget;
        const ssize_t count = send(connection->socket_fd,
                                   chunk->data + connection->output_offset,
                                   to_write, MSG_NOSIGNAL);
        if (count < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;
            return -1;
        }
        if (count == 0) {
            errno = EPIPE;
            return -1;
        }
        connection->output_offset += (size_t)count;
        connection->pending_bytes -= (size_t)count;
        budget -= (size_t)count;
        if (connection->output_offset == chunk->length) {
            connection->output_head = chunk->next;
            if (connection->output_head == NULL) connection->output_tail = NULL;
            free(chunk->data);
            free(chunk);
            connection->output_offset = 0;
        }
    }
    return 0;
}

static void accept_connections(Server *server)
{
    for (unsigned accepted = 0; accepted < 64; ++accepted) {
        const int client_fd = accept4(server->listener_fd, NULL, NULL,
                                      SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (client_fd < 0) {
            if (errno == EINTR || errno == ECONNABORTED) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) return;
            print_system_error("accept4");
            return;
        }
        if (server->connection_count >= server->options.max_connections ||
            server->next_connection_id == UINT64_MAX) {
            fprintf(stderr, "[INFO] stage=reject reason=%s\n",
                    server->connection_count >= server->options.max_connections
                        ? "max_connections" : "connection_id_exhausted");
            close(client_fd);
            continue;
        }
        const uint64_t id = server->next_connection_id++;
        if (watch_fd(server, EPOLL_CTL_ADD, client_fd, id, EPOLLIN | EPOLLRDHUP) != 0) {
            print_system_error("epoll_ctl(ADD client)");
            close(client_fd);
            continue;
        }
        Connection *connection = &server->connections[server->connection_count++];
        memset(connection, 0, sizeof(*connection));
        connection->socket_fd = client_fd;
        connection->id = id;
        frame_decoder_init(&connection->decoder);
        fprintf(stderr, "[INFO] conn=%" PRIu64 " stage=accept\n", id);
    }
}

static int server_init(Server *server, const Options *options)
{
    memset(server, 0, sizeof(*server));
    server->listener_fd = -1;
    server->epoll_fd = -1;
    server->signal_fd = -1;
    server->options = *options;
    server->next_connection_id = 2;
    server->connections = calloc(options->max_connections, sizeof(*server->connections));
    if (server->connections == NULL) return -1;
    server->listener_fd = make_ipv4_socket();
    if (server->listener_fd < 0) return -1;
    int reuse = 1;
    if (setsockopt(server->listener_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) != 0) return -1;
    struct sockaddr_in address;
    if (set_ipv4_address(options->host, options->port, &address) != 0) return -1;
    if (bind(server->listener_fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        listen(server->listener_fd, 128) != 0) return -1;
    socklen_t address_size = sizeof(address);
    if (getsockname(server->listener_fd, (struct sockaddr *)&address, &address_size) != 0) return -1;
    server->options.port = ntohs(address.sin_port);

    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    if (sigprocmask(SIG_BLOCK, &mask, NULL) != 0) return -1;
    server->signal_fd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);
    if (server->signal_fd < 0) return -1;
    server->epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    if (server->epoll_fd < 0) return -1;
    if (watch_fd(server, EPOLL_CTL_ADD, server->listener_fd, 0, EPOLLIN) != 0 ||
        watch_fd(server, EPOLL_CTL_ADD, server->signal_fd, 1, EPOLLIN) != 0) return -1;
    return 0;
}

static void server_destroy(Server *server)
{
    for (size_t index = 0; index < server->connection_count; ++index) {
        connection_destroy(&server->connections[index]);
    }
    free(server->connections);
    if (server->listener_fd >= 0) close(server->listener_fd);
    if (server->epoll_fd >= 0) close(server->epoll_fd);
    if (server->signal_fd >= 0) close(server->signal_fd);
}

static int server_run(Server *server)
{
    printf("Listening on %s:%u\n", server->options.host, server->options.port);
    fflush(stdout);
    struct epoll_event events[64];
    while (true) {
        const int event_count = epoll_wait(server->epoll_fd, events, 64, -1);
        if (event_count < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        for (int event_index = 0; event_index < event_count; ++event_index) {
            const uint64_t id = events[event_index].data.u64;
            if (id == 1) {
                fprintf(stderr, "[INFO] stage=shutdown connections=%zu\n", server->connection_count);
                return 0;
            }
            if (id == 0) {
                accept_connections(server);
                continue;
            }
            const size_t connection_index = find_connection(server, id);
            if (connection_index == server->connection_count) continue;
            Connection *connection = &server->connections[connection_index];
            const uint32_t flags = events[event_index].events;
            int failed = (flags & (EPOLLERR | EPOLLHUP | EPOLLRDHUP)) != 0;
            if (failed) errno = ECONNRESET;
            if (!failed && (flags & EPOLLIN) != 0 &&
                read_requests(server, id, connection) != 0) failed = 1;
            if (!failed && (flags & EPOLLOUT) != 0 && write_responses(connection) != 0) failed = 1;
            if (!failed) {
                const uint32_t interest = EPOLLIN | EPOLLRDHUP |
                    (connection->output_head == NULL ? 0U : EPOLLOUT);
                if (watch_fd(server, EPOLL_CTL_MOD, connection->socket_fd, id, interest) != 0) failed = 1;
            }
            if (failed) {
                const char *reason = errno == ENOBUFS ? "发送缓冲达到上限" :
                    (errno == EPROTO ? "协议帧或request_id非法" :
                     (errno == ECONNRESET ? "连接断开" : strerror(errno == 0 ? ECONNRESET : errno)));
                fprintf(stderr, "[INFO] conn=%" PRIu64 " stage=close reason=%s\n",
                        id, reason);
                remove_connection(server, connection_index);
            }
        }
    }
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        puts("agent-server [--config PATH] [--host IPv4] [--port 0..65535]\n"
             "             [--max-connections N] [--max-pending-bytes N]");
        return 0;
    }
    Options options;
    char error[256] = {0};
    if (parse_options(argc, argv, &options, error, sizeof(error)) != 0) {
        fprintf(stderr, "agent-server: %s\n", error);
        return 2;
    }
    Server server;
    if (server_init(&server, &options) != 0) {
        print_system_error("agent-server 初始化失败");
        server_destroy(&server);
        return 1;
    }
    const int result = server_run(&server);
    if (result != 0) print_system_error("agent-server");
    server_destroy(&server);
    return result == 0 ? 0 : 1;
}
