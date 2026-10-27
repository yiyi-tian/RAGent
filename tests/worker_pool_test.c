#define _POSIX_C_SOURCE 200809L
#include "server/worker_pool.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static void submit(WorkerPool *pool, uint64_t id)
{
    const char *body = "{\"query\":\"hello\"}";
    WorkItem item = {0};
    item.connection_id = 3;
    item.frame.type = MESSAGE_CHAT_REQUEST;
    item.frame.request_id = id;
    item.frame.body_length = strlen(body);
    item.frame.body = malloc(item.frame.body_length);
    assert(item.frame.body);
    memcpy(item.frame.body, body, item.frame.body_length);
    assert(worker_pool_submit(pool, &item));
}

static void pause_briefly(void)
{
    struct timespec delay = {
        .tv_sec = 0,
        .tv_nsec = 1000000,
    };
    nanosleep(&delay, NULL);
}

int main(void)
{
    WorkerPool *pool = worker_pool_create(2, 8, 1);
    assert(pool);
    submit(pool, 1);
    submit(pool, 2);
    submit(pool, 3);
    unsigned tries = 0;
    while (!worker_pool_test_waiting_results(pool) && tries++ < 3000) pause_briefly();
    assert(worker_pool_test_waiting_results(pool) > 0);
    unsigned received = 0;
    tries = 0;
    while (received < 3 && tries++ < 3000) {
        WorkItem result;
        if (!worker_pool_take(pool, &result)) { pause_briefly(); continue; }
        assert(result.connection_id == 3 && result.frame.type == MESSAGE_RESPONSE);
        frame_destroy(&result.frame);
        ++received;
    }
    assert(received == 3);
    submit(pool, 4);
    submit(pool, 5);
    tries = 0;
    while (!worker_pool_test_waiting_results(pool) && tries++ < 3000) pause_briefly();
    assert(worker_pool_test_waiting_results(pool) > 0);
    worker_pool_destroy(pool);
    return 0;
}
