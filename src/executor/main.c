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
    if (request != NULL && json_object_object_length(request) == 2 &&
        json_object_object_get_ex(request, "target", &target) &&
        json_object_is_type(target, json_type_string) &&
        strlen(json_object_get_string(target)) == (size_t)json_object_get_string_len(target) &&
        json_object_object_get_ex(request, "timeout_ms", &timeout) &&
        json_object_is_type(timeout, json_type_int) &&
        json_object_get_int64(timeout) >= 1 &&
        json_object_get_int64(timeout) <= COMMAND_TIMEOUT_MAX_MS && !cancelled) {
        command_diagnose(json_object_get_string(target), (unsigned)json_object_get_int(timeout),
                         &cancelled, &result);
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
