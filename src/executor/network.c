#define _GNU_SOURCE
#include "executor/network.h"
#include "executor/command.h"
#include "executor/owner.h"
#include "protocol/protocol.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

typedef struct {
    char scheme[6];
    char host[254];
    unsigned port;
} Destination;

static bool destination(const char *grant, Destination *out)
{
    char extra;
    char port[8];
    if (strlen(grant) > 280 ||
        sscanf(grant, "%5[a-z]://%253[a-zA-Z0-9.-]:%7[0-9]%c", out->scheme,
               out->host, port, &extra) != 3 ||
        (strcmp(out->scheme, "http") != 0 && strcmp(out->scheme, "https") != 0 &&
         strcmp(out->scheme, "ssh") != 0)) {
        return false;
    }
    unsigned long number = strtoul(port, NULL, 10);
    if (number == 0 || number > 65535) {
        return false;
    }
    out->port = (unsigned)number;
    struct in_addr ip;
    if (inet_aton(out->host, &ip) != 0 || strchr(out->host, '.') == NULL) {
        return false;
    }
    size_t label = 0;
    for (size_t index = 0; out->host[index] != '\0'; ++index) {
        unsigned char byte = (unsigned char)out->host[index];
        if (byte == '.') {
            if (label == 0 || label > 63 || out->host[index - 1] == '-') {
                return false;
            }
            label = 0;
        } else {
            if (label == 0 && byte == '-') {
                return false;
            }
            ++label;
            out->host[index] = (char)tolower(byte);
        }
    }
    return label > 0 && label <= 63 && out->host[strlen(out->host) - 1] != '-';
}

bool network_grant_valid(const char *grant)
{
    Destination parsed;
    return destination(grant, &parsed);
}

static int docker(const char *socket,
    char *const command[],
    int64_t deadline,
    const volatile sig_atomic_t *cancelled,
    CommandResult *result)
{
    char endpoint[512];
    if (snprintf(endpoint, sizeof(endpoint), "unix://%s", socket) >= (int)sizeof(endpoint)) {
        return -1;
    }
    char *argv[96] = { "docker", "--host", endpoint };
    size_t count = 3;
    for (size_t index = 0; command[index] != NULL; ++index) {
        if (count == 95) {
            return -1;
        }
        argv[count++] = command[index];
    }
    return command_capture("/usr/bin/docker",
        argv,
        NULL,
        0,
        65536,
        command_time_left(deadline, 10000),
        cancelled,
        result);
}

bool network_resolve_image(
    const char *socket, char image[72], const volatile sig_atomic_t *cancelled)
{
    CommandResult result;
    char *args[] = { "image", "inspect", "ragent-network-gateway:v2", "--format", "{{.Id}}", NULL };
    if (docker(socket, args, 0, cancelled, &result) != 0) {
        return false;
    }
    result.output[strcspn(result.output, "\r\n")] = '\0';
    if (strlen(result.output) != 71 || strncmp(result.output, "sha256:", 7) != 0) {
        return false;
    }
    memcpy(image, result.output, 72);
    return true;
}

static int configuration(const char *runtime, json_object *grants)
{
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/squid.conf", runtime) >= (int)sizeof(path)) {
        return -1;
    }
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0444);
    if (fd < 0) {
        return -1;
    }
    FILE *file = fdopen(fd, "w");
    if (file == NULL) {
        close(fd);
        return -1;
    }
    fputs("http_port 127.0.0.1:3128 ssl-bump cert=/run/gateway.crt key=/run/gateway.key "
          "generate-host-certificates=off\n"
          "visible_hostname ragent-command-gateway\n"
          "pid_filename none\npinger_enable off\ncache_log /dev/null\naccess_log none\n"
          "cache_store_log none\ncache deny all\ncache_mem 8 MB\n"
          "shutdown_lifetime 0 seconds\nconnect_timeout 10 seconds\n"
          "request_timeout 30 seconds\nread_timeout 120 seconds\n"
          "forward_max_tries 4\nacl web proto HTTP\nacl tunnel method CONNECT\n"
          "acl step1 at_step SslBump1\nacl step2 at_step SslBump2\n"
          "external_acl_type sni_match ttl=0 negative_ttl=0 children-max=2 "
          "%ssl::>sni %>rd /usr/local/libexec/ragent-sni-check\n"
          "acl same_sni external sni_match\n",
        file);
    size_t count = json_object_array_length(grants);
    for (size_t index = 0; index < count; ++index) {
        Destination grant;
        json_object *item = json_object_array_get_idx(grants, index);
        if (!json_object_is_type(item, json_type_string)
            || !destination(json_object_get_string(item), &grant)) {
            fclose(file);
            return -1;
        }
        fprintf(file,
            "acl host%zu dstdomain -n %s\nacl port%zu port %u\n"
            "acl grant%zu all-of host%zu port%zu %stunnel%s\n",
            index,
            grant.host,
            index,
            grant.port,
            index,
            index,
            index,
            strcmp(grant.scheme, "http") == 0 ? "!" : "",
            strcmp(grant.scheme, "http") == 0 ? " web" : "");
    }
    fputs("acl approved any-of", file);
    for (size_t index = 0; index < count; ++index) {
        fprintf(file, " grant%zu", index);
    }
    fputs("\nhttp_access deny !approved\n"
          "acl private dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 "
          "169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 "
          "192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 "
          "224.0.0.0/4 240.0.0.0/4 ::/128 ::1/128 ::ffff:0:0/96 "
          "64:ff9b::/96 100::/64 2001:db8::/32 fc00::/7 fe80::/10 ff00::/8\n"
          "http_access deny private\nhttp_access allow approved\nhttp_access deny all\n",
        file);
    for (size_t index = 0; index < count; ++index) {
        Destination grant;
        destination(json_object_get_string(json_object_array_get_idx(grants, index)), &grant);
        if (strcmp(grant.scheme, "ssh") == 0) {
            fprintf(file, "ssl_bump splice step1 grant%zu\n", index);
        }
    }
    fputs("ssl_bump peek step1\nssl_bump splice step2 same_sni\n"
          "ssl_bump terminate all\non_unsupported_protocol respond all\n",
        file);
    bool good = !ferror(file);
    return fclose(file) == 0 && good ? 0 : -1;
}

static int relay_start(NetworkGateway *gateway, const char *socket, int64_t deadline)
{
    char listen[4200];
    char target[1024];
    snprintf(
        listen, sizeof(listen), "UNIX-LISTEN:%s,fork,max-children=16,mode=0600", gateway->socket);
    char expected[256];
    snprintf(expected, sizeof(expected), "/run/user/%lu/ragent-sandbox-docker.sock",
             (unsigned long)getuid());
    if (strcmp(socket, expected) != 0) {
        return -1;
    }
    // EXEC只分词并execvp，不调用Shell；参数全部来自固定本机路径和随机容器名。
    snprintf(target, sizeof(target),
        "EXEC:\"/usr/bin/docker --host unix://%s exec -i %s "
        "/usr/bin/socat STDIO TCP:127.0.0.1:3128\",nofork", socket, gateway->name);
    pid_t parent = getpid();
    pid_t child = fork();
    if (child < 0) {
        return -1;
    }
    if (child == 0) {
        if (setpgid(0, 0) != 0 || prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent) {
            _exit(127);
        }
        int null = open("/dev/null", O_RDWR);
        if (null < 0) {
            _exit(127);
        }
        for (int fd = 0; fd < 3; ++fd) {
            if (dup2(null, fd) < 0) {
                _exit(127);
            }
        }
        // 调用进程的工作区、连接等描述符均为CLOEXEC，不传给代理。
        char *argv[] = { "socat", listen, target, NULL };
        char *env[] = { "PATH=/usr/bin:/bin", "LC_ALL=C", NULL };
        execve("/usr/bin/socat", argv, env);
        _exit(127);
    }
    gateway->relay = child;
    for (unsigned attempt = 0; attempt < 100 && command_time_left(deadline, 1) > 0; ++attempt) {
        struct stat info;
        if (lstat(gateway->socket, &info) == 0 && S_ISSOCK(info.st_mode)) {
            return 0;
        }
        if (waitpid(child, NULL, WNOHANG) == child) {
            gateway->relay = 0;
            return -1;
        }
        usleep(20000);
    }
    return -1;
}

int network_start(const char *socket,
    const char *image,
    const char *runtime,
    json_object *grants,
    NetworkGateway *gateway,
    int64_t deadline,
    const volatile sig_atomic_t *cancelled)
{
    memset(gateway, 0, sizeof(*gateway));
    if (!json_object_is_type(grants, json_type_array) || json_object_array_length(grants) == 0
        || json_object_array_length(grants) > 32 || configuration(runtime, grants) != 0) {
        return -1;
    }
    unsigned char random[16];
    if (getrandom(random, sizeof(random), 0) != sizeof(random)) {
        return -1;
    }
    strcpy(gateway->name, "ragent-command-");
    for (size_t index = 0; index < sizeof(random); ++index) {
        snprintf(gateway->name + 15 + index * 2, 3, "%02x", random[index]);
    }
    snprintf(gateway->socket, sizeof(gateway->socket), "%s/network.sock", runtime);
    char config[4200];
    char cert[4200];
    char key[4200];
    snprintf(config,
        sizeof(config),
        "type=bind,src=%s/squid.conf,dst=/etc/squid/squid.conf,readonly",
        runtime);
    snprintf(
        cert, sizeof(cert), "type=bind,src=%s/gateway.crt,dst=/run/gateway.crt,readonly", runtime);
    snprintf(
        key, sizeof(key), "type=bind,src=%s/gateway.key,dst=/run/gateway.key,readonly", runtime);
    // 证书仅供窥视ClientHello初始化SSL上下文；不签发站点证书、不改变客户端信任库。
    char cert_path[4096];
    char key_path[4096];
    snprintf(cert_path, sizeof(cert_path), "%s/gateway.crt", runtime);
    snprintf(key_path, sizeof(key_path), "%s/gateway.key", runtime);
    char *openssl[] = {
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=ragent.invalid",
        "-keyout",
        key_path,
        "-out",
        cert_path,
        NULL,
    };
    CommandResult captured;
    if (command_capture("/usr/bin/openssl", openssl, NULL, 0, 65536, 10000, cancelled, &captured)
        != 0) {
        return -1;
    }
    if (chmod(key_path, 0444) != 0 || chmod(cert_path, 0444) != 0) {
        return -1;
    }
    json_object *owner = owner_current();
    if (owner == NULL) {
        return -1;
    }
    char label[1024];
    snprintf(label,
        sizeof(label),
        "org.ragent.owner=%s",
        json_object_to_json_string_ext(owner, JSON_C_TO_STRING_PLAIN));
    json_object_put(owner);
    char *create[] = {
        "create",
        "--name",
        gateway->name,
        "--label",
        "org.ragent.command=true",
        "--label",
        label,
        "--read-only",
        "--user",
        "13:13",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--memory",
        "128m",
        "--memory-swap",
        "128m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "32",
        "--tmpfs",
        "/run/squid:rw,nosuid,nodev,size=4m,mode=1777",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=8m,mode=1777",
        "--mount",
        config,
        "--mount",
        cert,
        "--mount",
        key,
        (char *)image,
        "/usr/sbin/squid",
        "-N",
        "-f",
        "/etc/squid/squid.conf",
        NULL,
    };
    // 创建超时也可能已在daemon中生效，清理路径始终尝试按唯一名称删除。
    gateway->created = true;
    if (docker(socket, create, deadline, cancelled, &captured) != 0) {
        return -1;
    }
    char *start[] = { "start", gateway->name, NULL };
    if (docker(socket, start, deadline, cancelled, &captured) != 0) {
        return -1;
    }
    char *check[] = {"exec", gateway->name, "/usr/bin/socat", "-T", "1", "-u",
                     "OPEN:/dev/null", "TCP:127.0.0.1:3128", NULL};
    for (unsigned attempt = 0; attempt < 10 && !*cancelled; ++attempt) {
        if (docker(socket, check, deadline, cancelled, &captured) == 0) {
            return relay_start(gateway, socket, deadline);
        }
        if (command_time_left(deadline, 1) == 0) {
            break;
        }
        usleep(50000);
    }
    return -1;
}

bool network_stop(const char *socket, const char *runtime, NetworkGateway *gateway)
{
    bool ok = true;
    if (gateway->relay > 0) {
        if (kill(-gateway->relay, SIGKILL) != 0 && errno != ESRCH) {
            ok = false;
        }
        while (waitpid(gateway->relay, NULL, 0) < 0 && errno == EINTR) { }
        gateway->relay = 0;
    }
    if (gateway->created) {
        volatile sig_atomic_t active = 0;
        CommandResult captured;
        char *remove[] = { "rm", "--force", gateway->name, NULL };
        if (docker(socket, remove, 0, &active, &captured) != 0) {
            char *check[] = { "container", "inspect", gateway->name, NULL };
            if (docker(socket, check, 0, &active, &captured) == 0
                || strstr(captured.output, "No such") == NULL) {
                ok = false;
            }
        }
    }
    const char *files[] = { "network.sock", "squid.conf", "gateway.crt", "gateway.key", NULL };
    for (size_t index = 0; files[index] != NULL; ++index) {
        char path[4096];
        if (snprintf(path, sizeof(path), "%s/%s", runtime, files[index]) >= (int)sizeof(path)
            || (unlink(path) != 0 && errno != ENOENT)) {
            ok = false;
        }
    }
    return ok;
}
