#ifndef RAGENT_SERVER_TASK_BRIDGE_H
#define RAGENT_SERVER_TASK_BRIDGE_H
#include "protocol/protocol.h"
#include <time.h>

typedef struct {
    json_object *workspace;
    json_object *state;
    uint64_t request_id;
    MessageType mode;
    char continuation[33];
    bool waiting;
    time_t touched;
    int64_t phase_ms;
    unsigned remaining_ms;
} TaskBridge;

/* 仅网络线程持有和调用；断连时释放。最多64个保留私有状态的续行任务，每个内部状态512 KiB。 */
void task_bridge_destroy(TaskBridge *bridge);
/* 0为普通请求，1为直接reply，2为已重写的工作任务，-1为不可恢复错误。 */
int task_bridge_input(TaskBridge *bridge, Frame *request, Frame *reply);
/* 处理工作线程的HTTP结果，内部状态永不直接发给客户端。 */
int task_bridge_output(TaskBridge *bridge, Frame *response);
/* 返回1表示已生成等待超时终态，0表示未到期，-1表示构造失败。 */
int task_bridge_expire(TaskBridge *bridge, Frame *response, time_t now);
#endif
