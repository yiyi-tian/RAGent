#define _GNU_SOURCE
#include "executor/observe.h"
#include "executor/policy.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/openat2.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
#include <tree_sitter/api.h>

extern const TSLanguage *tree_sitter_bash(void);

static const char *relative_cwd = ".";

static int allow_path(int rules, int path, __u64 rights)
{
    struct landlock_path_beneath_attr rule = {
        .allowed_access = rights,
        .parent_fd = path,
    };
    return (int)syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH, &rule, 0);
}

static int allow_tree(int rules, int directory, unsigned depth)
{
    if (depth > 256 || allow_path(rules, directory, LANDLOCK_ACCESS_FS_READ_DIR) != 0) {
        return -1;
    }
    int copy = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (copy < 0) {
        return -1;
    }
    DIR *stream = fdopendir(copy);
    if (stream == NULL) {
        close(copy);
        return -1;
    }
    int status = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(stream);
        if (entry == NULL) {
            if (errno != 0) {
                status = -1;
            }
            break;
        }
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0
            || policy_protected_path(entry->d_name)) {
            continue;
        }
        int file = openat(directory, entry->d_name, O_PATH | O_NOFOLLOW | O_CLOEXEC);
        struct stat info;
        if (file < 0 || fstat(file, &info) != 0) {
            if (file >= 0) {
                close(file);
            }
            status = -1;
            break;
        }
        if (S_ISDIR(info.st_mode)) {
            status = allow_tree(rules, file, depth + 1);
        } else if (S_ISREG(info.st_mode) && info.st_nlink == 1) {
            status = allow_path(rules, file, LANDLOCK_ACCESS_FS_READ_FILE);
        }
        close(file);
        if (status != 0) {
            break;
        }
    }
    closedir(stream);
    return status;
}

static int filesystem_boundary(int root)
{
    int abi = (int)syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 3) {
        errno = ENOTSUP;
        return -1;
    }
    struct landlock_ruleset_attr attr = {
        .handled_access_fs = LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_WRITE_FILE
            | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
            | LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE
            | LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_DIR
            | LANDLOCK_ACCESS_FS_MAKE_REG | LANDLOCK_ACCESS_FS_MAKE_SOCK
            | LANDLOCK_ACCESS_FS_MAKE_FIFO | LANDLOCK_ACCESS_FS_MAKE_BLOCK
            | LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REFER | LANDLOCK_ACCESS_FS_TRUNCATE,
    };
    int rules = (int)syscall(SYS_landlock_create_ruleset, &attr, sizeof(attr), 0);
    if (rules < 0) {
        return -1;
    }
    int status = allow_tree(rules, root, 0);
    const char *const runtime[] = {
        "/lib",
        "/lib64",
        "/usr/lib",
        "/etc/ld.so.cache",
        "/dev/null",
        NULL,
    };
    for (size_t index = 0; status == 0 && runtime[index] != NULL; ++index) {
        int file = open(runtime[index], O_PATH | O_CLOEXEC);
        if (file < 0 && errno == ENOENT) {
            continue;
        }
        if (file < 0) {
            status = -1;
            break;
        }
        struct stat info;
        if (fstat(file, &info) != 0) {
            status = -1;
        } else {
            __u64 rights = LANDLOCK_ACCESS_FS_READ_FILE;
            if (strcmp(runtime[index], "/dev/null") == 0) {
                rights |= LANDLOCK_ACCESS_FS_WRITE_FILE;
            }
            if (S_ISDIR(info.st_mode)) {
                rights |= LANDLOCK_ACCESS_FS_READ_DIR;
            }
            status = allow_path(rules, file, rights);
        }
        close(file);
    }
    // ELF解释器需要执行权限，库目录中的其他程序不应因此获得执行权。
#if defined(__x86_64__)
    const char *loader = "/lib64/ld-linux-x86-64.so.2";
#elif defined(__aarch64__)
    const char *loader = "/lib/ld-linux-aarch64.so.1";
#else
#error 不支持的Landlock执行架构
#endif
    if (status == 0) {
        int file = open(loader, O_PATH | O_CLOEXEC);
        status = file < 0
            ? -1
            : allow_path(rules, file, LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_EXECUTE);
        if (file >= 0) {
            close(file);
        }
    }
    const char *const *programs = policy_observer_names();
    for (size_t index = 0; status == 0 && programs[index] != NULL; ++index) {
        char path[128];
        snprintf(path, sizeof(path), "/usr/bin/%s", programs[index]);
        int file = open(path, O_PATH | O_CLOEXEC);
        if (file < 0) {
            if (errno == ENOENT) {
                continue;
            }
            status = -1;
            break;
        }
        status = allow_path(rules, file, LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE);
        close(file);
    }
    if (status == 0) {
        status = (int)syscall(SYS_landlock_restrict_self, rules, 0);
    }
    close(rules);
    return status;
}

#define DENY_SYSCALL(number)                                                                       \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, number, 0, 1),                                             \
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)

static int syscall_boundary(void)
{
#if defined(__x86_64__)
    const unsigned architecture = AUDIT_ARCH_X86_64;
#elif defined(__aarch64__)
    const unsigned architecture = AUDIT_ARCH_AARCH64;
#else
    errno = ENOTSUP;
    return -1;
#endif
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, architecture, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
        DENY_SYSCALL(SYS_socket),
        DENY_SYSCALL(SYS_socketpair),
        DENY_SYSCALL(SYS_connect),
        DENY_SYSCALL(SYS_sendto),
        DENY_SYSCALL(SYS_sendmsg),
        DENY_SYSCALL(SYS_ptrace),
        DENY_SYSCALL(SYS_process_vm_readv),
        DENY_SYSCALL(SYS_process_vm_writev),
        DENY_SYSCALL(SYS_pidfd_getfd),
        DENY_SYSCALL(SYS_kill),
        DENY_SYSCALL(SYS_tkill),
        DENY_SYSCALL(SYS_tgkill),
        DENY_SYSCALL(SYS_pidfd_send_signal),
        DENY_SYSCALL(SYS_mount),
        DENY_SYSCALL(SYS_umount2),
        DENY_SYSCALL(SYS_unshare),
        DENY_SYSCALL(SYS_setns),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {
        .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
        .filter = filter,
    };
    return prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program);
}

static int wait_child(pid_t pid)
{
    int status;
    pid_t result;
    do {
        result = waitpid(pid, &status, 0);
    } while (result < 0 && errno == EINTR);
    if (result != pid) {
        return 126;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 128 + WTERMSIG(status);
}

static int execute_node(TSNode node, const char *source, int root, unsigned depth);

static int start_child(
    TSNode node, const char *source, int root, int input, int output, unsigned depth, pid_t *pid)
{
    pid_t parent = getpid();
    *pid = fork();
    if (*pid < 0) {
        return -1;
    }
    if (*pid == 0) {
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent
            || (input != STDIN_FILENO && dup2(input, STDIN_FILENO) < 0)
            || (output != STDOUT_FILENO && dup2(output, STDOUT_FILENO) < 0)) {
            _exit(126);
        }
        if (input > STDERR_FILENO) {
            close(input);
        }
        if (output > STDERR_FILENO && output != input) {
            close(output);
        }
        _exit(execute_node(node, source, root, depth + 1));
    }
    return 0;
}

/* 引号处理复用策略模块产生的argv，执行时仍重新分析整条原文。 */
static bool has_glob(TSNode argument, const char *source)
{
    if (strcmp(ts_node_type(argument), "word") != 0) {
        return false;
    }
    size_t end = ts_node_end_byte(argument);
    for (size_t index = ts_node_start_byte(argument); index < end; ++index) {
        if (source[index] == '\\') {
            ++index;
        } else if (strchr("*?[", source[index]) != NULL) {
            return true;
        }
    }
    return false;
}

static int append_argument(char *argv[256], size_t *used, const char *word, int root)
{
    if (*used >= 220) {
        errno = E2BIG;
        return -1;
    }
    const char *candidate = strchr(word, '=');
    candidate = candidate == NULL ? word : candidate + 1;
    // 文件操作数的链接解析由openat2限定；Landlock仍负责实际读取时的权限。
    if (*candidate != '-' && *candidate != '\0') {
        struct open_how how = {
            .flags = O_PATH | O_CLOEXEC,
            .resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS,
        };
        char relative[8192];
        if (snprintf(relative, sizeof(relative), "%s/%s", relative_cwd, candidate)
            >= (int)sizeof(relative)) {
            errno = ENAMETOOLONG;
            return -1;
        }
        if (*candidate == '/') {
            errno = EXDEV;
            return -1;
        }
        int file = (int)syscall(SYS_openat2, root, relative, &how, sizeof(how));
        if (file >= 0) {
            close(file);
        } else if (errno != ENOENT && errno != ENOTDIR) {
            return -1;
        }
    }
    argv[*used] = strdup(word);
    if (argv[*used] == NULL) {
        return -1;
    }
    ++*used;
    return 0;
}

static int execute_program(TSNode node, const char *source, int root)
{
    char *fragment = strndup(
        source + ts_node_start_byte(node), ts_node_end_byte(node) - ts_node_start_byte(node));
    if (fragment == NULL) {
        return 126;
    }
    json_object *policy = command_policy(fragment);
    free(fragment);
    json_object *steps = json_object_object_get(policy, "programs");
    json_object *step = json_object_array_get_idx(steps, 0);
    json_object *arguments = json_object_object_get(step, "argv");
    size_t count = json_object_array_length(arguments);
    char *argv[256] = { 0 };
    int status = 126;
    const char *name
        = count == 0 ? "" : json_object_get_string(json_object_array_get_idx(arguments, 0));
    const char *base = strrchr(name, '/');
    base = base == NULL ? name : base + 1;
    char path[128];
    snprintf(path, sizeof(path), "/usr/bin/%s", base);
    size_t used = 0;
    argv[used++] = strdup(base);
    if (strcmp(base, "git") == 0) {
        const char *fixed[] = {
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            NULL,
        };
        for (size_t index = 0; fixed[index] != NULL; ++index) {
            argv[used++] = strdup(fixed[index]);
        }
    }
    for (size_t index = 1; index < count; ++index) {
        const char *word = json_object_get_string(json_object_array_get_idx(arguments, index));
        TSNode argument = ts_node_named_child(node, (uint32_t)index);
        if (has_glob(argument, source)) {
            char *pattern = strndup(source + ts_node_start_byte(argument),
                ts_node_end_byte(argument) - ts_node_start_byte(argument));
            if (pattern == NULL) {
                goto cleanup;
            }
            glob_t expanded = { 0 };
            int matched = glob(pattern, 0, NULL, &expanded);
            free(pattern);
            if (matched == GLOB_NOMATCH) {
                matched = append_argument(argv, &used, word, root);
            } else if (matched == 0) {
                for (size_t item = 0; item < expanded.gl_pathc; ++item) {
                    if (append_argument(argv, &used, expanded.gl_pathv[item], root) != 0) {
                        matched = -1;
                        break;
                    }
                }
            }
            globfree(&expanded);
            if (matched != 0) {
                goto cleanup;
            }
        } else if (append_argument(argv, &used, word, root) != 0) {
            fputs("操作数超出授权工作区或不可访问\n", stderr);
            goto cleanup;
        }
        if (strcmp(base, "git") == 0 && index == 1 && strcmp(word, "diff") == 0) {
            argv[used++] = strdup("--no-ext-diff");
            argv[used++] = strdup("--no-textconv");
        }
    }
    if (strcmp(base, "git") == 0 && count >= 2) {
        const char *operation = json_object_get_string(json_object_array_get_idx(arguments, 1));
        if (strcmp(operation, "diff") == 0) {
            bool separator = false;
            for (size_t index = 2; index < count; ++index) {
                const char *word
                    = json_object_get_string(json_object_array_get_idx(arguments, index));
                separator = separator || strcmp(word, "--") == 0;
            }
            if (!separator) {
                argv[used++] = strdup("--");
                argv[used++] = strdup(".");
            }
            const char *const *excluded = policy_protected_names();
            for (size_t index = 0; excluded[index] != NULL; ++index) {
                if (used >= 253) {
                    goto cleanup;
                }
                char pattern[256];
                snprintf(pattern, sizeof(pattern), ":(exclude,glob)**/%s", excluded[index]);
                argv[used++] = strdup(pattern);
                snprintf(pattern, sizeof(pattern), ":(exclude,glob)**/%s/**", excluded[index]);
                argv[used++] = strdup(pattern);
            }
        }
    }
    for (size_t index = 0; index < used; ++index) {
        if (argv[index] == NULL) {
            goto cleanup;
        }
    }
    char *environment[] = {
        "PATH=/usr/bin:/bin",
        "LC_ALL=C",
        "HOME=/nonexistent",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_TERMINAL_PROMPT=0",
        NULL,
    };
    execve(path, argv, environment);
    perror("只读程序启动失败");
cleanup:
    for (size_t index = 0; index < used; ++index) {
        free(argv[index]);
    }
    json_object_put(policy);
    return status;
}

static int execute_node(TSNode node, const char *source, int root, unsigned depth)
{
    if (depth > 128) {
        return 126;
    }
    const char *type = ts_node_type(node);
    if (strcmp(type, "command") == 0) {
        return execute_program(node, source, root);
    }
    uint32_t count = ts_node_named_child_count(node);
    if (strcmp(type, "pipeline") == 0) {
        pid_t children[32];
        size_t started = 0;
        int input = STDIN_FILENO;
        for (uint32_t index = 0; index < count; ++index) {
            int channel[2] = { -1, -1 };
            bool last = index + 1 == count;
            if ((!last && pipe2(channel, O_CLOEXEC) != 0) || started == 32
                || start_child(ts_node_named_child(node, index),
                       source,
                       root,
                       input,
                       last ? STDOUT_FILENO : channel[1],
                       depth,
                       &children[started])
                    != 0) {
                if (channel[0] >= 0) {
                    close(channel[0]);
                    close(channel[1]);
                }
                if (input != STDIN_FILENO) {
                    close(input);
                }
                for (size_t child = 0; child < started; ++child) {
                    wait_child(children[child]);
                }
                return 126;
            }
            ++started;
            if (input != STDIN_FILENO) {
                close(input);
            }
            if (!last) {
                close(channel[1]);
                input = channel[0];
            }
        }
        int status = 0;
        for (size_t index = 0; index < started; ++index) {
            status = wait_child(children[index]);
        }
        return status;
    }
    int status = 0;
    uint32_t tokens = ts_node_child_count(node);
    const char *condition = NULL;
    for (uint32_t index = 0; index < tokens; ++index) {
        TSNode child = ts_node_child(node, index);
        if (!ts_node_is_named(child)) {
            if (strcmp(ts_node_type(child), "&&") == 0 || strcmp(ts_node_type(child), "||") == 0) {
                condition = ts_node_type(child);
            }
            continue;
        }
        if (strcmp(ts_node_type(child), "comment") == 0) {
            continue;
        }
        bool skip = condition != NULL
            && ((strcmp(condition, "&&") == 0 && status != 0)
                || (strcmp(condition, "||") == 0 && status == 0));
        condition = NULL;
        if (!skip) {
            pid_t pid;
            if (start_child(child, source, root, STDIN_FILENO, STDOUT_FILENO, depth, &pid) != 0) {
                return 126;
            }
            status = wait_child(pid);
        }
    }
    return status;
}

int observe_restrict(int root)
{
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 || filesystem_boundary(root) != 0) {
        return -1;
    }
    return syscall_boundary();
}

int observe_command(const char *command, int root, const char *cwd)
{
    relative_cwd = cwd;
    json_object *policy = command_policy(command);
    bool allowed = json_object_get_int(json_object_object_get(policy, "level")) == 1
        && json_object_object_get(policy, "denial_reason") == NULL;
    json_object_put(policy);
    if (!allowed) {
        fputs("该命令不是可直接执行的观察类命令\n", stderr);
        return 126;
    }
    struct rlimit memory = {
        .rlim_cur = 512 * 1024 * 1024,
        .rlim_max = 512 * 1024 * 1024,
    };
    struct rlimit cpu = {
        .rlim_cur = 10,
        .rlim_max = 10,
    };
    TSParser *parser = ts_parser_new();
    TSTree *tree = NULL;
    if (root >= 0 && parser != NULL && ts_parser_set_language(parser, tree_sitter_bash())) {
        tree = ts_parser_parse_string(parser, NULL, command, (uint32_t)strlen(command));
    }
    if (tree == NULL || setrlimit(RLIMIT_AS, &memory) != 0 || setrlimit(RLIMIT_CPU, &cpu) != 0
        || observe_restrict(root) != 0) {
        perror("宿主机只读边界不可用");
        ts_tree_delete(tree);
        ts_parser_delete(parser);
        return 126;
    }
    int result = execute_node(ts_tree_root_node(tree), command, root, 0);
    ts_tree_delete(tree);
    ts_parser_delete(parser);
    return result;
}
