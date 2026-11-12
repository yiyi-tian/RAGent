#ifndef RAGENT_EXECUTOR_NETWORK_H
#define RAGENT_EXECUTOR_NETWORK_H
#include <json-c/json.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>

typedef struct {
    char name[80];
    char socket[4096];
    pid_t relay;
    bool created;
} NetworkGateway;

/* 授权为本地UI产生的scheme://domain:port数组；不接受IP、通配符和隐式子域。 */
bool network_grant_valid(const char *grant);
bool network_resolve_image(
    const char *socket, char image[72], const volatile sig_atomic_t *cancelled);
int network_start(const char *docker_socket,
    const char *image,
    const char *runtime,
    json_object *grants,
    NetworkGateway *gateway,
    int64_t deadline,
    const volatile sig_atomic_t *cancelled);
bool network_stop(const char *docker_socket, const char *runtime, NetworkGateway *gateway);
#endif
