#include "executor/network.h"
#include <assert.h>

int main(void)
{
    const char *accepted[] = {
        "https://example.com:443",
        "http://example.com:80",
        "ssh://git.example.com:22",
        "https://EXAMPLE.com:8443",
        NULL,
    };
    const char *rejected[] = {
        "https://127.0.0.1:443",
        "https://127.1:443",
        "https://0x7f000001:443",
        "https://[::1]:443",
        "https://*.example.com:443",
        "https://example.com",
        "https://example.com:443/path",
        "https://user@example.com:443",
        "https://example.com:0",
        "https://example.com:65536",
        "https://example.com:4294967739",
        "https://example.com:+443",
        "https://example.com:-443",
        "https://example.com.:443",
        "https://-bad.example.com:443",
        "https://bad-.example.com:443",
        "https://bad..example.com:443",
        "ftp://example.com:21",
        NULL,
    };
    for (unsigned index = 0; accepted[index] != NULL; ++index) {
        assert(network_grant_valid(accepted[index]));
    }
    for (unsigned index = 0; rejected[index] != NULL; ++index) {
        assert(!network_grant_valid(rejected[index]));
    }
    return 0;
}
