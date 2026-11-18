#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static bool normalize(char *host)
{
    size_t length = strlen(host);
    if (length == 0 || length > 253 || strcmp(host, "-") == 0) {
        return false;
    }
    if (host[length - 1] == '.') {
        host[--length] = '\0';
    }
    for (size_t index = 0; index < length; ++index) {
        unsigned char byte = (unsigned char)host[index];
        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z')
                || (byte >= '0' && byte <= '9') || byte == '-' || byte == '.')) {
            return false;
        }
        host[index] = (char)tolower(byte);
    }
    return true;
}

int main(void)
{
    char line[1024];
    while (fgets(line, sizeof(line), stdin) != NULL) {
        char sni[254];
        char target[254];
        char data[2];
        char extra[2];
        bool valid = strchr(line, '\n') != NULL
            && sscanf(line, "%253s %253s %1s %1s", sni, target, data, extra) == 3
            && strcmp(data, "-") == 0 && normalize(sni) && normalize(target)
            && strcmp(sni, target) == 0;
        // 不能在无SNI时退回CONNECT目标，否则TLS请求可能绕过域名一致性检查。
        puts(valid ? "OK" : "ERR");
        if (fflush(stdout) != 0) {
            return 1;
        }
        if (strchr(line, '\n') == NULL) {
            int byte;
            do {
                byte = getchar();
            } while (byte != '\n' && byte != EOF);
        }
    }
    return ferror(stdin) ? 1 : 0;
}
