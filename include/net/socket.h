#ifndef RAGENT_SOCKET_H
#define RAGENT_SOCKET_H

#include <netinet/in.h>

int make_ipv4_socket(void);
int set_ipv4_address(const char *host, unsigned port, struct sockaddr_in *address);
int parse_unsigned(const char *value, unsigned minimum, unsigned maximum, unsigned *result);
void print_system_error(const char *operation);

#endif
