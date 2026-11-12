#define _POSIX_C_SOURCE 200809L
#include "executor/command.h"
#include "protocol/protocol.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t cancelled = 0;

static void stop_command(int signal_number)
{
    (void)signal_number;
    cancelled = 1;
}

int main(void)
{
    struct sigaction action = {0};
    action.sa_handler = stop_command;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0 || sigaction(SIGINT, &action, NULL) != 0) {
        return 1;
    }
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        return 1;
    }
    unsigned char input[4096];
    size_t length = 0;
    while (length < sizeof(input) && !cancelled) {
        ssize_t count = read(STDIN_FILENO, input + length, sizeof(input) - length);
        if (count == 0) {
            break;
        }
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return 1;
        }
        length += (size_t)count;
    }
    json_object *request = length < sizeof(input) ? json_parse_object(input, length) : NULL;
    json_object *target = NULL;
    json_object *timeout = NULL;
    CommandResult result = {
        .code = "COMMAND_NOT_ALLOWED",
        .exit_code = -1,
    };
    if (request != NULL &&
        json_object_object_get_ex(request, "target", &target) &&
        json_object_is_type(target, json_type_string) &&
        strlen(json_object_get_string(target)) == (size_t)json_object_get_string_len(target) &&
        json_object_object_get_ex(request, "timeout_ms", &timeout) &&
        json_object_is_type(timeout, json_type_int) &&
        json_object_get_int64(timeout) >= 1 &&
        json_object_get_int64(timeout) <= COMMAND_TIMEOUT_MAX_MS && !cancelled) {
        const char *name = json_object_get_string(target);
        unsigned milliseconds = (unsigned)json_object_get_int(timeout);
        int fields = json_object_object_length(request);
        if (strcmp(name, "process_detail") == 0 && fields == 3) {
            json_object *pid = json_object_object_get(request, "pid");
            if (pid != NULL && json_object_is_type(pid, json_type_int) &&
                json_object_get_int64(pid) > 0 && json_object_get_int64(pid) <= 2147483647) {
                char number[24];
                snprintf(number, sizeof(number), "%lld", (long long)json_object_get_int64(pid));
                char *arguments[] = {
                    "ps",
                    "-p",
                    number,
                    "-o",
                    "pid,ppid,stat,pcpu,pmem,etimes,comm",
                    NULL,
                };
                command_run("/usr/bin/ps", arguments, milliseconds, &cancelled, &result);
            }
        } else if (strcmp(name, "listeners") == 0 && fields == 3) {
            json_object *protocol = json_object_object_get(request, "protocol");
            if (protocol != NULL && json_object_is_type(protocol, json_type_string)) {
                const char *value = json_object_get_string(protocol);
                const char *flag = NULL;
                if (strcmp(value, "tcp") == 0) {
                    flag = "-ltn";
                } else if (strcmp(value, "udp") == 0) {
                    flag = "-lun";
                } else if (strcmp(value, "all") == 0) {
                    flag = "-ltun";
                }
                if (flag != NULL && strlen(value) == (size_t)json_object_get_string_len(protocol)) {
                    char *arguments[] = {"ss", (char *)flag, NULL};
                    command_run("/usr/bin/ss", arguments, milliseconds, &cancelled, &result);
                }
            }
        } else if (fields == 2) {
            command_diagnose(name, milliseconds, &cancelled, &result);
        }
    }
    if (request != NULL) {
        json_object_put(request);
    }
    json_object *reply = json_object_new_object();
    if (reply == NULL) {
        return 1;
    }
    json_object_object_add(reply, "ok", json_object_new_boolean(result.code == NULL));
    json_object_object_add(reply, "code",
                           json_object_new_string(result.code == NULL ? "" : result.code));
    json_object_object_add(reply, "output",
                           json_object_new_string_len(result.output, (int)result.output_length));
    json_object_object_add(reply, "exit_code", json_object_new_int(result.exit_code));
    json_object_object_add(reply, "signal", json_object_new_int(result.signal_number));
    const char *encoded = json_object_to_json_string_ext(reply, JSON_C_TO_STRING_PLAIN);
    int status = puts(encoded) < 0 || fflush(stdout) != 0 ? 1 : 0;
    json_object_put(reply);
    return status;
}
