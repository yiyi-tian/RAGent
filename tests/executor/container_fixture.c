#define _GNU_SOURCE
#include "executor/container.h"
#include "protocol/protocol.h"
#include "executor/network.h"
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t cancelled;

static void stop(int number)
{
    (void)number;
    cancelled = 1;
}

int main(int argc, char **argv)
{
    if ((argc != 4 && argc != 5) || signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        return 2;
    }
    int root = open(argv[1], O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (root < 0) {
        return 2;
    }
    struct sigaction action = { 0 };
    action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0) {
        close(root);
        return 2;
    }
    ContainerResult *outcome = calloc(1, sizeof(*outcome));
    if (outcome == NULL) {
        close(root);
        return 2;
    }
    CommandAuthorization authorization;
    authorization_init(&authorization);
    if (argc == 5) {
        json_object *grants = json_parse_object((const unsigned char *)argv[4], strlen(argv[4]));
        if (grants == NULL) {
            return 2;
        }
        json_object *network = json_object_object_get(grants, "network");
        json_object *environment = json_object_object_get(grants, "environment");
        if (network != NULL) {
            json_object_put(authorization.network);
            authorization.network = json_object_get(network);
        }
        if (environment != NULL) {
            json_object_put(authorization.environment);
            authorization.environment = json_object_get(environment);
        }
        json_object_put(grants);
        if (json_object_array_length(authorization.network) > 0
            && !network_resolve_image(argv[3], authorization.gateway_image, &cancelled)) {
            return 2;
        }
    }
    int status = container_command(
        argv[2], root, ".", argv[3], NULL, &authorization, NULL, NULL, &cancelled, outcome);
    authorization_destroy(&authorization);
    json_object *result = json_object_new_object();
    json_object_object_add(result, "status", json_object_new_int(status));
    json_object_object_add(result, "invoked", json_object_new_boolean(outcome->invoked));
    json_object_object_add(
        result, "cleanup_failed", json_object_new_boolean(outcome->cleanup_failed));
    json_object_object_add(result,
        "output",
        json_object_new_string_len(outcome->command.output, (int)outcome->command.output_length));
    json_object_object_add(result, "exit_code", json_object_new_int(outcome->command.exit_code));
    puts(json_object_to_json_string_ext(result, JSON_C_TO_STRING_PLAIN));
    json_object_put(result);
    free(outcome);
    close(root);
    return 0;
}
