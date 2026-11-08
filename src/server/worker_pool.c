#define _GNU_SOURCE
#include "server/worker_pool.h"
#include "server/agent_http.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdatomic.h>
#include <time.h>
#include <stdlib.h>
#include <sys/eventfd.h>
#include <unistd.h>

struct WorkerLifetime {
    atomic_uint references;
    atomic_bool cancelled;
};

WorkerLifetime *worker_lifetime_create(void)
{
    WorkerLifetime *lifetime = malloc(sizeof(*lifetime));
    if (lifetime != NULL) {
        atomic_init(&lifetime->references, 1);
        atomic_init(&lifetime->cancelled, false);
    }
    return lifetime;
}

void worker_lifetime_cancel(WorkerLifetime *lifetime)
{
    if (lifetime != NULL) {
        atomic_store(&lifetime->cancelled, true);
    }
}

bool worker_lifetime_cancelled(const WorkerLifetime *lifetime)
{
    return lifetime != NULL && atomic_load(&lifetime->cancelled);
}

void worker_lifetime_release(WorkerLifetime *lifetime)
{
    if (lifetime != NULL && atomic_fetch_sub(&lifetime->references, 1) == 1) {
        free(lifetime);
    }
}

typedef struct {
    WorkItem item;
    uint64_t enqueued_ns;
} QueuedTask;

struct WorkerPool {
    pthread_mutex_t mutex;
    pthread_cond_t tasks_ready;
    pthread_cond_t result_space;
    pthread_t *threads;
    unsigned started;
    QueuedTask *tasks;
    size_t task_capacity;
    size_t task_head;
    size_t task_count;
    WorkItem *results;
    size_t result_capacity;
    size_t result_head;
    size_t result_count;
    bool stopping;
    size_t active_count;
    unsigned timeout_ms;
#ifdef RAGENT_TESTING
    unsigned waiting_results;
#endif
    int event_fd;
};

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static void log_task_event(const WorkItem *item, const char *event)
{
    if (getenv("RAGENT_METRICS") != NULL) {
        fprintf(stderr, "[TASK] conn=%llu req=%llu event=%s ns=%llu\n",
                (unsigned long long)item->connection_id,
                (unsigned long long)item->frame.request_id,
                event, (unsigned long long)monotonic_ns());
    }
}

static void notify_network(WorkerPool *pool)
{
    uint64_t one = 1;
    ssize_t result;
    do {
        result = write(pool->event_fd, &one, sizeof(one));
    } while (result < 0 && errno == EINTR);
    /* 计数器饱和时本身已处于可读状态。 */
    if (result < 0 && errno != EAGAIN) {
        fprintf(stderr, "eventfd通知失败: %d\n", errno);
    }
}

static void *worker_main(void *argument)
{
    WorkerPool *pool = argument;
    for (;;) {
        pthread_mutex_lock(&pool->mutex);
        while (!pool->stopping && pool->task_count == 0) {
            pthread_cond_wait(&pool->tasks_ready, &pool->mutex);
        }
        if (pool->stopping) {
            pthread_mutex_unlock(&pool->mutex);
            break;
        }
        WorkItem item = pool->tasks[pool->task_head].item;
        uint64_t enqueued_ns = pool->tasks[pool->task_head].enqueued_ns;
        pool->task_head = (pool->task_head + 1) % pool->task_capacity;
        --pool->task_count;
        ++pool->active_count;
        pthread_mutex_unlock(&pool->mutex);

#ifdef RAGENT_TESTING
        const char *delay_text = getenv("RAGENT_TEST_DELAY_MS");
        if (delay_text != NULL &&
            (getenv("RAGENT_TEST_DELAY_FIRST_ONLY") == NULL || item.frame.request_id == 1)) {
            unsigned delay = (unsigned)strtoul(delay_text, NULL, 10);
            fprintf(stderr,
                    "[TEST] stage=task_start req=%llu\n",
                    (unsigned long long)item.frame.request_id);
            struct timespec pause = {
                .tv_sec = delay / 1000,
                .tv_nsec = (long)(delay % 1000) * 1000000L,
            };
            while (nanosleep(&pause, &pause) != 0 && errno == EINTR) {
            }
        }
#endif
        if (worker_lifetime_cancelled(item.lifetime)) {
            log_task_event(&item, "discarded");
            fprintf(stderr, "[INFO] conn=%llu req=%llu stage=discard reason=disconnect\n",
                    (unsigned long long)item.connection_id,
                    (unsigned long long)item.frame.request_id);
            frame_destroy(&item.frame);
            worker_lifetime_release(item.lifetime);
            pthread_mutex_lock(&pool->mutex);
            --pool->active_count;
            pthread_mutex_unlock(&pool->mutex);
            continue;
        }
        uint64_t started_ns = monotonic_ns();
        Frame response = {0};
        int status;
        uint64_t elapsed_ms = (started_ns - enqueued_ns) / 1000000ULL;
        if (started_ns == 0 || enqueued_ns == 0) {
            status = frame_make_error(item.frame.request_id, "INTERNAL_ERROR",
                                      "无法读取请求时钟", &response);
        } else if (elapsed_ms >= pool->timeout_ms) {
            status = frame_make_error(item.frame.request_id, "UPSTREAM_TIMEOUT",
                                      "请求排队后已超过时限，未执行", &response);
        } else {
            unsigned remaining_ms = pool->timeout_ms - (unsigned)elapsed_ms;
            status = agent_http_request_controlled(&item.frame, remaining_ms,
                                                   item.lifetime, &response);
        }
        pthread_mutex_lock(&pool->mutex);
        --pool->active_count;
        log_task_event(&item, "completed");
        size_t queued = pool->task_count;
        size_t active = pool->active_count;
        size_t results = pool->result_count;
        pthread_mutex_unlock(&pool->mutex);
        if (getenv("RAGENT_METRICS") != NULL && enqueued_ns != 0) {
            fprintf(stderr,
                    "[PERF] conn=%llu req=%llu queue_ms=%.3f http_ms=%.3f "
                    "queued=%zu active=%zu results=%zu\n",
                    (unsigned long long)item.connection_id,
                    (unsigned long long)item.frame.request_id,
                    (started_ns - enqueued_ns) / 1000000.0,
                    (monotonic_ns() - started_ns) / 1000000.0, queued, active, results);
        }
        uint64_t request_id = item.frame.request_id;
        frame_destroy(&item.frame);
        worker_lifetime_release(item.lifetime);
        item.lifetime = NULL;
        if (status != 0 &&
            frame_make_error(request_id, "INTERNAL_ERROR", "处理请求失败", &response) != 0) {
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

WorkerPool *worker_pool_create(unsigned threads, unsigned task_capacity, unsigned result_capacity,
                               unsigned timeout_ms)
{
    WorkerPool *pool = calloc(1, sizeof(*pool));
    if (!pool) {
        return NULL;
    }
    pool->event_fd = -1;
    pool->timeout_ms = timeout_ms;
    if (pthread_mutex_init(&pool->mutex, NULL) != 0) {
        goto fail_pool;
    }
    if (pthread_cond_init(&pool->tasks_ready, NULL) != 0) {
        goto fail_mutex;
    }
    if (pthread_cond_init(&pool->result_space, NULL) != 0) {
        goto fail_tasks_cond;
    }
    pool->threads = calloc(threads, sizeof(*pool->threads));
    pool->tasks = calloc(task_capacity, sizeof(*pool->tasks));
    pool->results = calloc(result_capacity, sizeof(*pool->results));
    pool->task_capacity = task_capacity;
    pool->result_capacity = result_capacity;
    pool->event_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (!pool->threads || !pool->tasks || !pool->results || pool->event_fd < 0) {
        goto fail_ready;
    }
    for (; pool->started < threads; ++pool->started) {
        if (pthread_create(&pool->threads[pool->started], NULL, worker_main, pool) != 0) {
            goto fail_ready;
        }
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

int worker_pool_event_fd(const WorkerPool *pool)
{
    return pool->event_fd;
}

bool worker_pool_submit(WorkerPool *pool, WorkItem *item)
{
    pthread_mutex_lock(&pool->mutex);
    if (pool->stopping || pool->task_count == pool->task_capacity) {
        pthread_mutex_unlock(&pool->mutex);
        return false;
    }
    size_t tail = (pool->task_head + pool->task_count) % pool->task_capacity;
    pool->tasks[tail].enqueued_ns = monotonic_ns();
    if (item->lifetime != NULL) {
        atomic_fetch_add(&item->lifetime->references, 1);
    }
    pool->tasks[tail].item = *item;
    item->frame.body = NULL;
    ++pool->task_count;
    log_task_event(item, "accepted");
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
    if (!pool) {
        return;
    }
    pthread_mutex_lock(&pool->mutex);
    pool->stopping = true;
    pthread_cond_broadcast(&pool->tasks_ready);
    pthread_cond_broadcast(&pool->result_space);
    pthread_mutex_unlock(&pool->mutex);
    for (unsigned i = 0; i < pool->started; ++i) {
        pthread_join(pool->threads[i], NULL);
    }
    if (getenv("RAGENT_METRICS") != NULL) {
        fprintf(stderr, "[POOL] shutdown queued=%zu active=%zu results=%zu\n",
                pool->task_count, pool->active_count, pool->result_count);
    }
    for (size_t i = 0; i < pool->task_count; ++i) {
        WorkItem *item = &pool->tasks[(pool->task_head + i) % pool->task_capacity].item;
        frame_destroy(&item->frame);
        worker_lifetime_release(item->lifetime);
    }
    for (size_t i = 0; i < pool->result_count; ++i) {
        frame_destroy(&pool->results[(pool->result_head + i) % pool->result_capacity].frame);
    }
    if (pool->event_fd >= 0) {
        close(pool->event_fd);
    }
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
