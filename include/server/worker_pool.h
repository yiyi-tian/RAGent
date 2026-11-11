#ifndef RAGENT_WORKER_POOL_H
#define RAGENT_WORKER_POOL_H

#include "protocol/protocol.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct WorkerPool WorkerPool;
typedef struct WorkerLifetime WorkerLifetime;
/* 网络线程持有初始引用；入队成功后线程池另持一份，关闭连接只标记失效。 */
WorkerLifetime *worker_lifetime_create(void);
void worker_lifetime_cancel(WorkerLifetime *lifetime);
bool worker_lifetime_cancelled(const WorkerLifetime *lifetime);
void worker_lifetime_release(WorkerLifetime *lifetime);
typedef struct {
    uint64_t connection_id;
    Frame frame;
    WorkerLifetime *lifetime;
} WorkItem;

/* timeout_ms 从成功入队开始计时，包含排队与业务执行，不含结果发送。
 * 创建失败返回 NULL；调用方通过 worker_pool_destroy 释放。
 */
WorkerPool *worker_pool_create(unsigned threads, unsigned task_capacity, unsigned result_capacity,
                               unsigned timeout_ms);
int worker_pool_event_fd(const WorkerPool *pool);
/* 成功时转移frame所有权，并保留可选lifetime的内部引用；不转移调用方的lifetime引用。
 * 失败时所有权均保持不变。 */
bool worker_pool_submit(WorkerPool *pool, WorkItem *item);
bool worker_pool_take(WorkerPool *pool, WorkItem *item);
bool worker_pool_has_results(WorkerPool *pool);
void worker_pool_destroy(WorkerPool *pool);
#ifdef RAGENT_TESTING
unsigned worker_pool_test_waiting_results(WorkerPool *pool);
#endif
#endif
