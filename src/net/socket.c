#include "net/socket.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int make_ipv4_socket(void)
{
    return socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
}

int set_ipv4_address(const char *host, unsigned port, struct sockaddr_in *address)
{
    memset(address, 0, sizeof(*address));
    address->sin_family = AF_INET;
    address->sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &address->sin_addr) != 1) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

int parse_unsigned(const char *value, unsigned minimum, unsigned maximum, unsigned *result)
{
    if (value == NULL || *value == '\0') {
        return -1;
    }
    unsigned long parsed = 0;
    for (const char *cursor = value; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return -1;
        }
        const unsigned long digit = (unsigned long)(*cursor - '0');
        if (parsed > (ULONG_MAX - digit) / 10UL) {
            return -1;
        }
        parsed = parsed * 10UL + digit;
    }
    if (parsed < minimum || parsed > maximum) {
        return -1;
    }
    *result = (unsigned)parsed;
    return 0;
}

void print_system_error(const char *operation)
{
    fprintf(stderr, "%s: %s\n", operation, strerror(errno));
}
