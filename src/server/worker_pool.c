#define _GNU_SOURCE
#include "server/worker_pool.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#ifdef RAGENT_TESTING
#include <time.h>
#endif
#include <stdlib.h>
#include <sys/eventfd.h>
#include <unistd.h>

struct WorkerPool {
    pthread_mutex_t mutex;
    pthread_cond_t tasks_ready;
    pthread_cond_t result_space;
    pthread_t *threads;
    unsigned started;
    WorkItem *tasks;
    size_t task_capacity;
    size_t task_head;
    size_t task_count;
    WorkItem *results;
    size_t result_capacity;
    size_t result_head;
    size_t result_count;
    bool stopping;
#ifdef RAGENT_TESTING
    unsigned waiting_results;
#endif
    int event_fd;
};

static void notify_network(WorkerPool *pool)
{
    uint64_t one = 1;
    ssize_t result;
    do {
        result = write(pool->event_fd, &one, sizeof(one));
    } while (result < 0 && errno == EINTR);
    /* 计数器饱和时本身已处于可读状态。 */
    if (result < 0 && errno != EAGAIN) fprintf(stderr, "eventfd通知失败: %d\n", errno);
}

static void *worker_main(void *argument)
{
    WorkerPool *pool = argument;
    for (;;) {
        pthread_mutex_lock(&pool->mutex);
        while (!pool->stopping && pool->task_count == 0)
            pthread_cond_wait(&pool->tasks_ready, &pool->mutex);
        if (pool->stopping) {
            pthread_mutex_unlock(&pool->mutex);
            break;
        }
        WorkItem item = pool->tasks[pool->task_head];
        pool->task_head = (pool->task_head + 1) % pool->task_capacity;
        --pool->task_count;
        pthread_mutex_unlock(&pool->mutex);

#ifdef RAGENT_TESTING
        const char *delay_text = getenv("RAGENT_TEST_DELAY_MS");
        if (delay_text != NULL && (getenv("RAGENT_TEST_DELAY_FIRST_ONLY") == NULL || item.frame.request_id == 1)) {
            unsigned delay = (unsigned)strtoul(delay_text, NULL, 10);
            fprintf(stderr, "[TEST] stage=task_start req=%llu\n", (unsigned long long)item.frame.request_id);
            struct timespec pause = {
                .tv_sec = delay / 1000,
                .tv_nsec = (long)(delay % 1000) * 1000000L,
            };
            while (nanosleep(&pause, &pause) != 0 && errno == EINTR) {}
        }
#endif
        Frame response = {0};
        int status = handle_fixed_request(&item.frame, &response);
        uint64_t request_id = item.frame.request_id;
        frame_destroy(&item.frame);
        if (status != 0 && frame_make_error(request_id, "INTERNAL_ERROR", "处理请求失败", &response) != 0) {
            response.request_id = request_id;
        }
        item.frame = response;

        pthread_mutex_lock(&pool->mutex);
        while (!pool->stopping && pool->result_count == pool->result_capacity) {
#ifdef RAGENT_TESTING
            ++pool->waiting_results;
#endif
            pthread_cond_wait(&pool->result_space, &pool->mutex);
#ifdef RAGENT_TESTING
            --pool->waiting_results;
#endif
        }
        if (pool->stopping) {
            pthread_mutex_unlock(&pool->mutex);
            frame_destroy(&item.frame);
            break;
        }
        size_t tail = (pool->result_head + pool->result_count) % pool->result_capacity;
        pool->results[tail] = item;
        ++pool->result_count;
        notify_network(pool);
        pthread_mutex_unlock(&pool->mutex);
    }
    return NULL;
}

WorkerPool *worker_pool_create(unsigned threads, unsigned task_capacity, unsigned result_capacity)
{
    WorkerPool *pool = calloc(1, sizeof(*pool));
    if (!pool) return NULL;
    pool->event_fd = -1;
    if (pthread_mutex_init(&pool->mutex, NULL) != 0) goto fail_pool;
    if (pthread_cond_init(&pool->tasks_ready, NULL) != 0) goto fail_mutex;
    if (pthread_cond_init(&pool->result_space, NULL) != 0) goto fail_tasks_cond;
    pool->threads = calloc(threads, sizeof(*pool->threads));
    pool->tasks = calloc(task_capacity, sizeof(*pool->tasks));
    pool->results = calloc(result_capacity, sizeof(*pool->results));
    pool->task_capacity = task_capacity;
    pool->result_capacity = result_capacity;
    pool->event_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (!pool->threads || !pool->tasks || !pool->results || pool->event_fd < 0) goto fail_ready;
    for (; pool->started < threads; ++pool->started) {
        if (pthread_create(&pool->threads[pool->started], NULL, worker_main, pool) != 0) goto fail_ready;
    }
    return pool;
fail_ready:
    worker_pool_destroy(pool);
    return NULL;
fail_tasks_cond:
    pthread_cond_destroy(&pool->tasks_ready);
fail_mutex:
    pthread_mutex_destroy(&pool->mutex);
fail_pool:
    free(pool);
    return NULL;
}

int worker_pool_event_fd(const WorkerPool *pool) { return pool->event_fd; }

bool worker_pool_submit(WorkerPool *pool, WorkItem *item)
{
    pthread_mutex_lock(&pool->mutex);
    if (pool->stopping || pool->task_count == pool->task_capacity) {
        pthread_mutex_unlock(&pool->mutex);
        return false;
    }
    size_t tail = (pool->task_head + pool->task_count) % pool->task_capacity;
    pool->tasks[tail] = *item;
    item->frame.body = NULL;
    ++pool->task_count;
    pthread_cond_signal(&pool->tasks_ready);
    pthread_mutex_unlock(&pool->mutex);
    return true;
}

bool worker_pool_take(WorkerPool *pool, WorkItem *item)
{
    pthread_mutex_lock(&pool->mutex);
    if (pool->result_count == 0) {
        pthread_mutex_unlock(&pool->mutex);
        return false;
    }
    *item = pool->results[pool->result_head];
    pool->result_head = (pool->result_head + 1) % pool->result_capacity;
    --pool->result_count;
    pthread_cond_signal(&pool->result_space);
    pthread_mutex_unlock(&pool->mutex);
    return true;
}

bool worker_pool_has_results(WorkerPool *pool)
{
    pthread_mutex_lock(&pool->mutex);
    bool has_results = pool->result_count != 0;
    pthread_mutex_unlock(&pool->mutex);
    return has_results;
}

void worker_pool_destroy(WorkerPool *pool)
{
    if (!pool) return;
    pthread_mutex_lock(&pool->mutex);
    pool->stopping = true;
    pthread_cond_broadcast(&pool->tasks_ready);
    pthread_cond_broadcast(&pool->result_space);
    pthread_mutex_unlock(&pool->mutex);
    for (unsigned i = 0; i < pool->started; ++i) pthread_join(pool->threads[i], NULL);
    for (size_t i = 0; i < pool->task_count; ++i)
        frame_destroy(&pool->tasks[(pool->task_head + i) % pool->task_capacity].frame);
    for (size_t i = 0; i < pool->result_count; ++i)
        frame_destroy(&pool->results[(pool->result_head + i) % pool->result_capacity].frame);
    if (pool->event_fd >= 0) close(pool->event_fd);
    free(pool->threads);
    free(pool->tasks);
    free(pool->results);
    pthread_cond_destroy(&pool->result_space);
    pthread_cond_destroy(&pool->tasks_ready);
    pthread_mutex_destroy(&pool->mutex);
    free(pool);
}

#ifdef RAGENT_TESTING
unsigned worker_pool_test_waiting_results(WorkerPool *pool)
{
    pthread_mutex_lock(&pool->mutex);
    unsigned count = pool->waiting_results;
    pthread_mutex_unlock(&pool->mutex);
    return count;
}
#endif
