#define _GNU_SOURCE
#include "protocol/protocol.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static int relay(void)
{
    pid_t parent = getpid();
    pid_t child = fork();
    if (child < 0) {
        return -1;
    }
    if (child == 0) {
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent) {
            _exit(127);
        }
        int null = open("/dev/null", O_RDWR | O_CLOEXEC);
        if (null < 0) {
            _exit(127);
        }
        for (int fd = 0; fd < 3; ++fd) {
            if (dup2(null, fd) < 0) {
                _exit(127);
            }
        }
        close(null);
        char *argv[] = {
            "socat",
            "TCP-LISTEN:3128,bind=127.0.0.1,reuseaddr,fork,max-children=16",
            "UNIX-CONNECT:/run/ragent/network.sock",
            NULL,
        };
        execv("/usr/bin/socat", argv);
        _exit(127);
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        return 2;
    }
    unsigned char *bytes = malloc(262145);
    if (bytes == NULL) {
        return 1;
    }
    int fd = open("/run/ragent/authorization.json", O_RDONLY | O_CLOEXEC);
    size_t used = 0;
    while (fd >= 0 && used < 262145) {
        ssize_t count = read(fd, bytes + used, 262145 - used);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            if (count < 0) {
                used = 262145;
            }
            break;
        }
        used += (size_t)count;
    }
    if (fd < 0) {
        free(bytes);
        return 1;
    }
    close(fd);
    json_object *config = used <= 262144 ? json_parse_object(bytes, used) : NULL;
    explicit_bzero(bytes, used);
    free(bytes);
    if (config == NULL) {
        return 1;
    }
    json_object *environment = json_object_object_get(config, "environment");
    if (!json_object_is_type(environment, json_type_object)) {
        json_object_put(config);
        return 1;
    }
    if (strcmp(argv[0], "/usr/local/libexec/ragent-askpass") == 0) {
        json_object *passphrase = json_object_object_get(environment, "RAGENT_SSH_PASSPHRASE");
        int status = json_object_is_type(passphrase, json_type_string) ? 0 : 1;
        if (status == 0) {
            puts(json_object_get_string(passphrase));
        }
        json_object_put(config);
        return status;
    }
    if (clearenv() != 0 || setenv("PATH", "/usr/local/bin:/usr/bin:/bin", 1) != 0
        || setenv("HOME", "/root", 1) != 0 || setenv("LC_ALL", "C.UTF-8", 1) != 0
        || setenv("GIT_TERMINAL_PROMPT", "0", 1) != 0 || setenv("PIP_USER", "1", 1) != 0
        || setenv("PIP_BREAK_SYSTEM_PACKAGES", "1", 1) != 0
        || setenv("PYTHONUSERBASE", "/workspace/.ragent-packages/python", 1) != 0) {
        json_object_put(config);
        return 1;
    }
    json_object_object_foreach(environment, name, value)
    {
        if (!json_object_is_type(value, json_type_string)
            || (strcmp(name, "RAGENT_SSH_PASSPHRASE") != 0
                && setenv(name, json_object_get_string(value), 1) != 0)) {
            json_object_put(config);
            return 1;
        }
    }
    bool network = json_object_get_boolean(json_object_object_get(config, "network"));
    if (mkdir("/root/.ssh", 0700) != 0 && errno != EEXIST) {
        json_object_put(config);
        return 1;
    }
    FILE *ssh = fopen("/root/.ssh/config", "wx");
    if (ssh == NULL) {
        json_object_put(config);
        return 1;
    }
    fputs("Host *\n  StrictHostKeyChecking yes\n  UserKnownHostsFile "
          "/run/ragent/credentials/known_hosts\n  GlobalKnownHostsFile /dev/null\n"
          "  IdentityAgent none\n  IdentitiesOnly yes\n  IdentityFile "
          "/run/ragent/credentials/ssh_key\n  PasswordAuthentication no\n"
          "  KbdInteractiveAuthentication no\n",
        ssh);
    if (network) {
        fputs("  ProxyCommand /usr/bin/socat STDIO "
              "PROXY:127.0.0.1:%h:%p,proxyport=3128\n",
            ssh);
    }
    if (fclose(ssh) != 0) {
        json_object_put(config);
        return 1;
    }
    if (json_object_object_get(environment, "RAGENT_SSH_PASSPHRASE") != NULL) {
        if (setenv("SSH_ASKPASS", "/usr/local/libexec/ragent-askpass", 1) != 0
            || setenv("SSH_ASKPASS_REQUIRE", "force", 1) != 0 || setenv("DISPLAY", ":0", 1) != 0) {
            json_object_put(config);
            return 1;
        }
    }
    if (network) {
        if (setenv("http_proxy", "http://127.0.0.1:3128", 1) != 0
            || setenv("https_proxy", "http://127.0.0.1:3128", 1) != 0
            || setenv("HTTP_PROXY", "http://127.0.0.1:3128", 1) != 0
            || setenv("HTTPS_PROXY", "http://127.0.0.1:3128", 1) != 0 || relay() != 0) {
            json_object_put(config);
            return 1;
        }
    }
    json_object_put(config);
    char *command[] = { "bash", "--noprofile", "--norc", "-c", argv[1], NULL };
    execv("/bin/bash", command);
    perror("执行已授权命令失败");
    return 127;
}
