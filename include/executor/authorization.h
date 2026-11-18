#ifndef RAGENT_EXECUTOR_AUTHORIZATION_H
#define RAGENT_EXECUTOR_AUTHORIZATION_H
#include <json-c/json.h>
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    json_object *network;
    json_object *environment;
    json_object *files;
    char gateway_image[72];
    unsigned timeout_ms;
    int64_t deadline_ms;
    int64_t approval_deadline_ms;
} CommandAuthorization;

void authorization_init(CommandAuthorization *authorization);
void authorization_destroy(CommandAuthorization *authorization);
/* 仅本地TTY可填写；文件使用已打开描述符固定，内容在私有运行目录快照。 */
bool authorization_prompt(CommandAuthorization *authorization);
bool authorization_timeout(CommandAuthorization *authorization);
int authorization_snapshot(const CommandAuthorization *authorization, const char *runtime);
bool authorization_cleanup(const char *runtime);
bool authorization_has_credentials(const CommandAuthorization *authorization);
#endif
