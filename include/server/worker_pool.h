#ifndef RAGENT_WORKER_POOL_H
#define RAGENT_WORKER_POOL_H

#include "protocol/protocol.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct WorkerPool WorkerPool;
typedef struct {
    uint64_t connection_id;
    Frame frame;
} WorkItem;

/* 提交成功时转移frame所有权；失败时调用者保留所有权。 */
WorkerPool *worker_pool_create(unsigned threads, unsigned task_capacity, unsigned result_capacity);
int worker_pool_event_fd(const WorkerPool *pool);
bool worker_pool_submit(WorkerPool *pool, WorkItem *item);
bool worker_pool_take(WorkerPool *pool, WorkItem *item);
bool worker_pool_has_results(WorkerPool *pool);
void worker_pool_destroy(WorkerPool *pool);
#ifdef RAGENT_TESTING
unsigned worker_pool_test_waiting_results(WorkerPool *pool);
#endif
#endif
