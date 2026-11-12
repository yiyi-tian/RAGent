#ifndef RAGENT_EXECUTOR_OWNER_H
#define RAGENT_EXECUTOR_OWNER_H
#include <json-c/json.h>
#include <stdbool.h>

/* PID、启动ticks与boot ID共同标识资源持有者，僵尸进程视为已退出。 */
json_object *owner_current(void);
bool owner_alive(json_object *identity);
int owner_save(const char *directory, json_object *identity);
#endif
